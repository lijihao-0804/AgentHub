param(
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot,
    [Parameter(Mandatory=$true)][string]$StatePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-NormalizedPath {
    param([Parameter(Mandatory=$true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd([char[]]@('\', '/'))
}

function Assert-PathDoesNotTraverseReparsePoint {
    param(
        [Parameter(Mandatory=$true)][string]$Root,
        [Parameter(Mandatory=$true)][string]$Target
    )

    $currentPath = $Target
    while ($true) {
        $attributes = $null
        try {
            $attributes = [System.IO.File]::GetAttributes($currentPath)
        }
        catch [System.IO.FileNotFoundException] {
            $attributes = $null
        }
        catch [System.IO.DirectoryNotFoundException] {
            $attributes = $null
        }

        if ($null -ne $attributes -and
            (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "Refusing to cross a reparse point in the portfolio state path: $currentPath"
        }
        if ([string]::Equals(
            $currentPath,
            $Root,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return
        }

        $parent = [System.IO.Directory]::GetParent($currentPath)
        if ($null -eq $parent) {
            throw 'Portfolio state escaped the workspace while checking reparse points.'
        }
        $currentPath = $parent.FullName
    }
}

function Assert-StateTreeHasNoReparsePoints {
    param([Parameter(Mandatory=$true)][string]$Path)

    $pending = [System.Collections.Generic.Stack[System.IO.DirectoryInfo]]::new()
    $pending.Push([System.IO.DirectoryInfo]::new($Path))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to harden a state directory that is a reparse point: $($directory.FullName)"
        }
        foreach ($child in $directory.EnumerateFileSystemInfos()) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to harden a state tree containing a reparse point: $($child.FullName)"
            }
            if (($child.Attributes -band [System.IO.FileAttributes]::Directory) -ne 0) {
                $pending.Push([System.IO.DirectoryInfo]::new($child.FullName))
            }
        }
    }
}

function Set-ProtectedDacl {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][bool]$IsDirectory,
        [Parameter(Mandatory=$true)][string[]]$AllowedSids
    )

    # Persist only DACL changes. Reusing Get-Acl through Set-Acl can try to
    # persist owner/audit sections and require SeSecurityPrivilege on a repeat
    # invocation, even though the user can change this directory's DACL.
    $acl = if ($IsDirectory) {
        [System.Security.AccessControl.DirectorySecurity]::new()
    } else {
        [System.Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)

    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    if ($IsDirectory) {
        $inheritance = (
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
    }
    foreach ($sidValue in $AllowedSids) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($sidValue),
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule) | Out-Null
    }
    $target = Get-Item -LiteralPath $Path -Force
    if ($PSVersionTable.PSEdition -eq 'Core') {
        [System.IO.FileSystemAclExtensions]::SetAccessControl($target, $acl)
    } else {
        $target.SetAccessControl($acl)
    }
}

function Assert-ProtectedDacl {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][bool]$IsDirectory,
        [Parameter(Mandatory=$true)][string[]]$AllowedSids
    )

    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "DACL inheritance remained enabled for $Path."
    }
    if (-not $acl.AreAccessRulesCanonical) {
        throw "DACL is not canonical for $Path."
    }

    $rules = @($acl.Access)
    if ($rules.Count -ne $AllowedSids.Count) {
        throw "DACL does not contain exactly the three permitted SIDs for $Path."
    }

    $actualSids = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($rule in $rules) {
        if ($rule.IsInherited) {
            throw "An inherited access rule remained on $Path."
        }
        if ($rule.AccessControlType -ne
            [System.Security.AccessControl.AccessControlType]::Allow) {
            throw "A non-allow access rule remained on $Path."
        }
        if ($rule.FileSystemRights -ne
            [System.Security.AccessControl.FileSystemRights]::FullControl) {
            throw "A permitted SID does not have full control on $Path."
        }
        $expectedInheritance = [System.Security.AccessControl.InheritanceFlags]::None
        if ($IsDirectory) {
            $expectedInheritance = (
                [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
            )
        }
        if ($rule.InheritanceFlags -ne $expectedInheritance -or
            $rule.PropagationFlags -ne
                [System.Security.AccessControl.PropagationFlags]::None) {
            throw "DACL inheritance flags are incorrect for $Path."
        }
        $sidValue = $rule.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        $actualSids.Add($sidValue) | Out-Null
    }

    foreach ($sidValue in $AllowedSids) {
        if (-not $actualSids.Contains($sidValue)) {
            throw "DACL is missing permitted SID $sidValue for $Path."
        }
    }
}

$workspace = Get-NormalizedPath -Path $WorkspaceRoot
$state = Get-NormalizedPath -Path $StatePath
$expectedState = Get-NormalizedPath -Path (Join-Path $workspace '.scratch/portfolio-closeout')
if (-not [string]::Equals(
    $state,
    $expectedState,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'StatePath must be the dedicated .scratch/portfolio-closeout directory.'
}
if (-not [System.IO.Directory]::Exists($workspace)) {
    throw "WorkspaceRoot does not exist: $workspace"
}

Assert-PathDoesNotTraverseReparsePoint -Root $workspace -Target $state
[System.IO.Directory]::CreateDirectory($state) | Out-Null
Assert-StateTreeHasNoReparsePoints -Path $state

$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSidSet = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($sidValue in @($currentSid, 'S-1-5-18', 'S-1-5-32-544')) {
    $allowedSidSet.Add($sidValue) | Out-Null
}
$allowedSids = @($allowedSidSet)

Set-ProtectedDacl -Path $state -IsDirectory $true -AllowedSids $allowedSids
Assert-ProtectedDacl -Path $state -IsDirectory $true -AllowedSids $allowedSids

foreach ($name in @('private-config.json', 'compose.env')) {
    $configPath = Join-Path $state $name
    if ([System.IO.Directory]::Exists($configPath)) {
        throw "Expected a configuration file but found a directory: $configPath"
    }
    if (-not [System.IO.File]::Exists($configPath)) {
        continue
    }
    if (([System.IO.File]::GetAttributes($configPath) -band
        [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to harden a configuration file that is a reparse point: $configPath"
    }
    Set-ProtectedDacl -Path $configPath -IsDirectory $false -AllowedSids $allowedSids
    Assert-ProtectedDacl -Path $configPath -IsDirectory $false -AllowedSids $allowedSids
}

Assert-StateTreeHasNoReparsePoints -Path $state
Write-Output 'Portfolio state DACL verified and protected.'
