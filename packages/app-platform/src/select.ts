export type SelectNavigationKey = "ArrowDown" | "ArrowUp" | "Home" | "End";

export function nextSelectIndex(
  currentIndex: number,
  optionCount: number,
  key: SelectNavigationKey,
): number {
  if (optionCount <= 0) {
    return -1;
  }
  if (key === "Home") {
    return 0;
  }
  if (key === "End") {
    return optionCount - 1;
  }
  if (key === "ArrowDown") {
    return (Math.max(currentIndex, -1) + 1) % optionCount;
  }
  return currentIndex <= 0 ? optionCount - 1 : currentIndex - 1;
}
