#requires -Version 5.1
<#
.SYNOPSIS
Convert a local scan to layout JSON for the AgentHub document upload form.
.EXAMPLE
powershell.exe -NoProfile -File scripts/convert-scan.ps1 scan.pdf scan.json -Language zh-Hans-CN
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)][string]$InputPath,
    [Parameter(Mandatory, Position=1)][string]$OutputPath,
    [string]$Language
)

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw 'Run this converter with Windows PowerShell 5.1 (powershell.exe).'
}
$inputFile = Get-Item -LiteralPath $InputPath
$outputFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
if ($inputFile.Length -gt 10MB -or $inputFile.Extension -notin '.pdf', '.png', '.jpg', '.jpeg') {
    throw 'Choose a PDF, PNG or JPEG file up to 10 MiB.'
}
if ([IO.Path]::GetExtension($outputFile) -ne '.json' -or (Test-Path -LiteralPath $outputFile)) {
    throw 'Choose a new .json output file.'
}

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Length -eq 1
}
$asResult = $methods | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }
$asAction = $methods | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' }
function Wait-Operation($Operation, [Type]$ResultType) {
    if ($ResultType) {
        $task = $asResult.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        return $task.GetAwaiter().GetResult()
    }
    $task = $asAction.Invoke($null, @($Operation))
    $null = $task.GetAwaiter().GetResult()
}

if ($Language) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
} else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if (-not $engine) {
    $available = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages.LanguageTag -join ', '
    throw "No matching Windows OCR language. Installed languages: $available"
}
$blocks = [Collections.Generic.List[object]]::new()
$charCount = 0
$file = Wait-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($inputFile.FullName)) ([Windows.Storage.StorageFile])
$pdf = $null
$pageCount = 1
if ($inputFile.Extension -eq '.pdf') {
    $pdf = Wait-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
    $pageCount = $pdf.PageCount
    if ($pageCount -gt 500) { throw 'PDF exceeds the 500-page limit.' }
}
for ($i = 0; $i -lt $pageCount; $i++) {
    $page = $null
    $stream = $null
    $bitmap = $null
    try {
        if ($null -ne $pdf) {
            $page = $pdf.GetPage($i)
            $options = [Windows.Data.Pdf.PdfPageRenderOptions]::new()
            $scale = [Math]::Min(2.5, 3200 / [Math]::Max($page.Size.Width, $page.Size.Height))
            $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Round($page.Size.Width * $scale))
            $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Round($page.Size.Height * $scale))
            $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
            Wait-Operation ($page.RenderToStreamAsync($stream, $options))
            $stream.Seek(0)
        } else {
            $stream = Wait-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        }
        $decoder = Wait-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $width, $height = $decoder.PixelWidth, $decoder.PixelHeight
        $limit = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
        if ($width -gt $limit -or $height -gt $limit -or [long]$width * $height -gt 20000000 -or $decoder.FrameCount -ne 1) {
            throw 'Use a single-frame image within the installed OCR size limit and 20 megapixels.'
        }
        $bitmap = Wait-Operation ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Wait-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        foreach ($line in $result.Lines) {
            if ([string]::IsNullOrWhiteSpace($line.Text) -or $line.Words.Count -eq 0) { continue }
            $charCount += $line.Text.Length
            if ($charCount -gt 2000000 -or $blocks.Count -ge 20000) { throw 'OCR result exceeds the document limits.' }
            $left, $top, $right, $bottom = $width, $height, 0, 0
            foreach ($word in $line.Words) {
                $box = $word.BoundingRect
                $left = [Math]::Min([double]$left, $box.X)
                $top = [Math]::Min([double]$top, $box.Y)
                $right = [Math]::Max([double]$right, $box.X + $box.Width)
                $bottom = [Math]::Max([double]$bottom, $box.Y + $box.Height)
            }
            # Windows inserts word separators between individual Han characters.
            $lineText = $line.Text -replace '(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])', ''
            $blocks.Add([ordered]@{
                type = 'text'; text = $lineText; page_idx = $i;
                bbox = @([int][Math]::Floor(1000*$left/$width), [int][Math]::Floor(1000*$top/$height),
                         [int][Math]::Ceiling(1000*$right/$width), [int][Math]::Ceiling(1000*$bottom/$height))
            })
        }
    } finally {
        if ($bitmap) { $bitmap.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($page) { $page.Dispose() }
    }
}
if ($blocks.Count -eq 0) { throw 'No text recognized. Check the language and image quality.' }
$json = ConvertTo-Json -InputObject $blocks.ToArray() -Depth 5 -Compress
$outputDir = [IO.Path]::GetDirectoryName($outputFile)
$temporary = Join-Path $outputDir ([IO.Path]::GetRandomFileName())
try {
    [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
    if ((Get-Item -LiteralPath $temporary).Length -gt 10MB) { throw 'Output exceeds the default 10 MiB upload limit.' }
    [IO.File]::Move($temporary, $outputFile)
} finally {
    if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
}
Write-Output "Created $outputFile ($pageCount pages, $($blocks.Count) recognized lines, $($engine.RecognizerLanguage.LanguageTag))."
