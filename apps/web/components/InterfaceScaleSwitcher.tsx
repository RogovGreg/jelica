"use client";

import { useRouter } from "next/navigation";

import { useI18n } from "@/components/I18nProvider";
import { useInterfaceScale } from "@/hooks/useInterfaceScale";
import { parseInterfaceScale } from "../../../packages/app-platform/src/theme";

export function InterfaceScaleSwitcher() {
  const router = useRouter();
  const { t } = useI18n();
  const { scale, setScale, options } = useInterfaceScale();

  return (
    <label className="input-field" htmlFor="interface-scale">
      <span>{t("settings.interface-scale")}</span>
      <select
        id="interface-scale"
        value={scale}
        onChange={(event) => {
          setScale(parseInterfaceScale(event.target.value));
          router.refresh();
        }}
      >
        {options.map((value) => <option key={value} value={value}>{value}%</option>)}
      </select>
    </label>
  );
}
