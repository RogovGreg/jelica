export const INTERFACE_SCALE_STEPS = [80, 100, 125, 150] as const;

export type InterfaceScale = (typeof INTERFACE_SCALE_STEPS)[number];
export type DocumentationPresentationTextSize = "small" | "standard" | "large";

export function parseInterfaceScale(value: string | null | undefined): InterfaceScale {
  const parsed = Number(value);
  return Number.isInteger(parsed) && INTERFACE_SCALE_STEPS.includes(parsed as InterfaceScale)
    ? parsed as InterfaceScale
    : 100;
}

export function interfaceScaleRatio(scale: InterfaceScale): number {
  return scale / 100;
}

export function documentationTextSizeForScale(scale: InterfaceScale): DocumentationPresentationTextSize {
  if (scale === 80) return "small";
  if (scale === 100) return "standard";
  return "large";
}
