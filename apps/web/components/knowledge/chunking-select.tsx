import { useI18n } from "@/i18n/provider";
import type { ChunkingStrategy } from "@/lib/api/knowledge";

export function selectedChunking(form: HTMLFormElement): ChunkingStrategy | undefined {
  const value = new FormData(form).get("chunking_strategy");
  return value === "fixed_v1" || value === "structural_v1" || value === "adaptive_v1" ? value : undefined;
}

export default function ChunkingSelect({ inherit = false, baseStrategy }: {
  inherit?: boolean;
  baseStrategy?: ChunkingStrategy;
}) {
  const { t } = useI18n();
  return (
    <label>
      {t("knowledge.chunking")}
      <select name="chunking_strategy" defaultValue={inherit ? "" : "fixed_v1"}>
        {inherit && (
          <option value="">
            {baseStrategy ? t("knowledge.chunkingFromBase", { strategy: t(`knowledge.${baseStrategy}`) }) : t("knowledge.chunkingDefault")}
          </option>
        )}
        <option value="fixed_v1">{t("knowledge.fixed_v1")}</option>
        <option value="structural_v1">{t("knowledge.structural_v1")}</option>
        <option value="adaptive_v1">{t("knowledge.adaptive_v1")}</option>
      </select>
    </label>
  );
}
