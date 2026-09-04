export type MafftOverrides = {
  strategy?: "auto" | "fft_ns_1" | "fft_ns_2" | "fft_ns_i" | "nw_ns_1" | "nw_ns_2" | "nw_ns_i" | "g_ins_i" | "l_ins_i" | "e_ins_i";
  direction_adjustment?: "none" | "fast" | "accurate";
  memory_mode?: "auto" | "save";
  threads?: "auto" | number;
  gap_open_penalty?: number;
  offset?: number;
  progressive_threads?: "auto" | "disabled" | number;
  iterative_threads?: "auto" | "disabled" | number;
};

export type AnalysisOverrides = {
  alignment?: {
    mode?: "compute" | "prealigned" | "none";
    engine?: "mafft";
    construction?: "joint" | "reference_guided";
    mafft?: MafftOverrides;
  };
  reference?: string;
  statistics?: { kmers?: string[]; kmer_strand?: "forward" | "reverse_complement" | "both" };
  comparative_analysis?: {
    enabled?: boolean;
    statistics?: { enabled?: boolean };
    sequence_differences?: {
      enabled?: boolean;
      substitutions?: boolean;
      insertions?: boolean;
      deletions?: boolean;
      symbol_policy?: { uracil_thymine_equivalent?: boolean };
    };
    reference?: { mode?: "auto" | "enabled" | "disabled" };
    pairwise?: {
      enabled?: boolean;
      all?: boolean;
      pairs_orientation?: "directed" | "bidirectional";
      groups?: string[][];
      pairs?: string[][];
    };
  };
  distance_matrix?: { enabled?: boolean; model?: "p_distance" };
  phylogenetic_tree?: { enabled?: boolean; method?: "neighbor_joining"; rooting?: "midpoint" };
  clade_detection?: { enabled?: boolean; method?: "max_pairwise_distance"; max_within_clade_distance?: number };
};

export class AnalysisValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AnalysisValidationError";
  }
}

export function validateAnalysisOverrides(value: unknown): AnalysisOverrides | null {
  if (value === null || value === undefined) return null;
  const root = object(value, "overrides");
  keys(root, ["alignment", "reference", "statistics", "comparative_analysis", "distance_matrix", "phylogenetic_tree", "clade_detection"], "overrides");
  const result: AnalysisOverrides = {};
  if (root.alignment !== undefined) result.alignment = validateAlignment(root.alignment);
  if (root.reference !== undefined) result.reference = text(root.reference, "overrides.reference");
  if (root.statistics !== undefined) result.statistics = validateStatistics(root.statistics);
  if (root.comparative_analysis !== undefined) result.comparative_analysis = validateComparative(root.comparative_analysis);
  if (root.distance_matrix !== undefined) result.distance_matrix = validateDistance(root.distance_matrix);
  if (root.phylogenetic_tree !== undefined) result.phylogenetic_tree = validateTree(root.phylogenetic_tree);
  if (root.clade_detection !== undefined) result.clade_detection = validateClades(root.clade_detection);
  return result;
}

export function serializeAnalysisOverrides(value: AnalysisOverrides | null | undefined): readonly string[] {
  if (value === null || value === undefined) return [];
  const validated = validateAnalysisOverrides(value);
  if (validated === null) return [];
  const result: string[] = [];
  visit(validated, "", result);
  return result;
}

function validateAlignment(value: unknown): NonNullable<AnalysisOverrides["alignment"]> {
  const source = object(value, "overrides.alignment");
  keys(source, ["mode", "engine", "construction", "mafft"], "overrides.alignment");
  const result: NonNullable<AnalysisOverrides["alignment"]> = {};
  if (source.mode !== undefined) result.mode = enumValue(source.mode, ["compute", "prealigned", "none"], "alignment.mode");
  if (source.engine !== undefined) result.engine = enumValue(source.engine, ["mafft"], "alignment.engine");
  if (source.construction !== undefined) result.construction = enumValue(source.construction, ["joint", "reference_guided"], "alignment.construction");
  if (source.mafft !== undefined) result.mafft = validateMafft(source.mafft);
  return result;
}

function validateMafft(value: unknown): MafftOverrides {
  const source = object(value, "overrides.alignment.mafft");
  keys(source, ["strategy", "direction_adjustment", "memory_mode", "threads", "gap_open_penalty", "offset", "progressive_threads", "iterative_threads"], "overrides.alignment.mafft");
  const result: MafftOverrides = {};
  if (source.strategy !== undefined) result.strategy = enumValue(source.strategy, ["auto", "fft_ns_1", "fft_ns_2", "fft_ns_i", "nw_ns_1", "nw_ns_2", "nw_ns_i", "g_ins_i", "l_ins_i", "e_ins_i"], "mafft.strategy");
  if (source.direction_adjustment !== undefined) result.direction_adjustment = enumValue(source.direction_adjustment, ["none", "fast", "accurate"], "mafft.direction_adjustment");
  if (source.memory_mode !== undefined) result.memory_mode = enumValue(source.memory_mode, ["auto", "save"], "mafft.memory_mode");
  if (source.threads !== undefined) result.threads = numberOrEnum(source.threads, ["auto"], "mafft.threads", 1);
  if (source.gap_open_penalty !== undefined) result.gap_open_penalty = number(source.gap_open_penalty, "mafft.gap_open_penalty", 0);
  if (source.offset !== undefined) result.offset = number(source.offset, "mafft.offset", 0);
  if (source.progressive_threads !== undefined) result.progressive_threads = numberOrEnum(source.progressive_threads, ["auto", "disabled"], "mafft.progressive_threads", 1);
  if (source.iterative_threads !== undefined) result.iterative_threads = numberOrEnum(source.iterative_threads, ["auto", "disabled"], "mafft.iterative_threads", 1);
  return result;
}

function validateStatistics(value: unknown): NonNullable<AnalysisOverrides["statistics"]> {
  const source = object(value, "overrides.statistics");
  keys(source, ["kmers", "kmer_strand"], "overrides.statistics");
  const result: NonNullable<AnalysisOverrides["statistics"]> = {};
  if (source.kmers !== undefined) {
    if (!Array.isArray(source.kmers)) throw invalid("statistics.kmers must be an array");
    result.kmers = source.kmers.map((item, index) => text(item, `statistics.kmers[${index}]`).toUpperCase());
  }
  if (source.kmer_strand !== undefined) result.kmer_strand = enumValue(source.kmer_strand, ["forward", "reverse_complement", "both"], "statistics.kmer_strand");
  return result;
}

function validateComparative(value: unknown): NonNullable<AnalysisOverrides["comparative_analysis"]> {
  const source = object(value, "overrides.comparative_analysis");
  keys(source, ["enabled", "statistics", "sequence_differences", "reference", "pairwise"], "overrides.comparative_analysis");
  const result: NonNullable<AnalysisOverrides["comparative_analysis"]> = {};
  if (source.enabled !== undefined) result.enabled = boolean(source.enabled, "comparative_analysis.enabled");
  if (source.statistics !== undefined) result.statistics = validateEnabled(source.statistics, "comparative_analysis.statistics");
  if (source.sequence_differences !== undefined) {
    const differences = object(source.sequence_differences, "comparative_analysis.sequence_differences");
    keys(differences, ["enabled", "substitutions", "insertions", "deletions", "symbol_policy"], "sequence_differences");
    const valueResult: NonNullable<NonNullable<AnalysisOverrides["comparative_analysis"]>["sequence_differences"]> = {};
    for (const field of ["enabled", "substitutions", "insertions", "deletions"] as const) if (differences[field] !== undefined) valueResult[field] = boolean(differences[field], `sequence_differences.${field}`);
    if (differences.symbol_policy !== undefined) {
      const policy = object(differences.symbol_policy, "sequence_differences.symbol_policy");
      keys(policy, ["uracil_thymine_equivalent"], "symbol_policy");
      valueResult.symbol_policy = { uracil_thymine_equivalent: boolean(policy.uracil_thymine_equivalent, "symbol_policy.uracil_thymine_equivalent") };
    }
    result.sequence_differences = valueResult;
  }
  if (source.reference !== undefined) {
    const reference = object(source.reference, "comparative_analysis.reference");
    keys(reference, ["mode"], "comparative_analysis.reference");
    result.reference = { mode: enumValue(reference.mode, ["auto", "enabled", "disabled"], "comparative_analysis.reference.mode") };
  }
  if (source.pairwise !== undefined) {
    const pairwise = object(source.pairwise, "comparative_analysis.pairwise");
    keys(pairwise, ["enabled", "all", "pairs_orientation", "groups", "pairs"], "comparative_analysis.pairwise");
    const pairwiseResult: NonNullable<NonNullable<AnalysisOverrides["comparative_analysis"]>["pairwise"]> = {};
    if (pairwise.enabled !== undefined) pairwiseResult.enabled = boolean(pairwise.enabled, "pairwise.enabled");
    if (pairwise.all !== undefined) pairwiseResult.all = boolean(pairwise.all, "pairwise.all");
    if (pairwise.pairs_orientation !== undefined) pairwiseResult.pairs_orientation = enumValue(pairwise.pairs_orientation, ["directed", "bidirectional"], "pairwise.pairs_orientation");
    for (const field of ["groups", "pairs"] as const) if (pairwise[field] !== undefined) pairwiseResult[field] = stringPairs(pairwise[field], `pairwise.${field}`);
    result.pairwise = pairwiseResult;
  }
  return result;
}

function validateEnabled(value: unknown, label: string): { enabled?: boolean } {
  const source = object(value, label);
  keys(source, ["enabled"], label);
  return source.enabled === undefined ? {} : { enabled: boolean(source.enabled, `${label}.enabled`) };
}

function validateDistance(value: unknown): NonNullable<AnalysisOverrides["distance_matrix"]> {
  const source = object(value, "overrides.distance_matrix");
  keys(source, ["enabled", "model"], "overrides.distance_matrix");
  return { ...(source.enabled === undefined ? {} : { enabled: boolean(source.enabled, "distance_matrix.enabled") }), ...(source.model === undefined ? {} : { model: enumValue(source.model, ["p_distance"], "distance_matrix.model") }) };
}

function validateTree(value: unknown): NonNullable<AnalysisOverrides["phylogenetic_tree"]> {
  const source = object(value, "overrides.phylogenetic_tree");
  keys(source, ["enabled", "method", "rooting"], "overrides.phylogenetic_tree");
  return { ...(source.enabled === undefined ? {} : { enabled: boolean(source.enabled, "phylogenetic_tree.enabled") }), ...(source.method === undefined ? {} : { method: enumValue(source.method, ["neighbor_joining"], "phylogenetic_tree.method") }), ...(source.rooting === undefined ? {} : { rooting: enumValue(source.rooting, ["midpoint"], "phylogenetic_tree.rooting") }) };
}

function validateClades(value: unknown): NonNullable<AnalysisOverrides["clade_detection"]> {
  const source = object(value, "overrides.clade_detection");
  keys(source, ["enabled", "method", "max_within_clade_distance"], "overrides.clade_detection");
  return { ...(source.enabled === undefined ? {} : { enabled: boolean(source.enabled, "clade_detection.enabled") }), ...(source.method === undefined ? {} : { method: enumValue(source.method, ["max_pairwise_distance"], "clade_detection.method") }), ...(source.max_within_clade_distance === undefined ? {} : { max_within_clade_distance: number(source.max_within_clade_distance, "clade_detection.max_within_clade_distance", 0, 1) }) };
}

function visit(value: object, prefix: string, result: string[]): void {
  for (const [key, child] of Object.entries(value)) {
    if (child === undefined || child === null) continue;
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof child === "object" && !Array.isArray(child)) visit(child, path, result);
    else result.push(`--${path}=${JSON.stringify(child)}`);
  }
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw invalid(`${label} must be an object`);
  return value as Record<string, unknown>;
}
function keys(value: Record<string, unknown>, allowed: readonly string[], label: string): void {
  for (const key of Object.keys(value)) if (!allowed.includes(key)) throw invalid(`${label}.${key} is not supported`);
}
function text(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim() === "") throw invalid(`${label} must be a non-empty string`);
  return value.trim();
}
function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw invalid(`${label} must be boolean`);
  return value;
}
function number(value: unknown, label: string, minimum: number, maximum = Number.POSITIVE_INFINITY): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) throw invalid(`${label} is outside the supported range`);
  return value;
}
function numberOrEnum<const T extends string>(value: unknown, allowed: readonly T[], label: string, minimum: number): T | number {
  if (typeof value === "string") return enumValue(value, allowed, label);
  return number(value, label, minimum);
}
function enumValue<const T extends string>(value: unknown, allowed: readonly T[], label: string): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) throw invalid(`${label} has an unsupported value`);
  return value as T;
}
function stringPairs(value: unknown, label: string): string[][] {
  if (!Array.isArray(value)) throw invalid(`${label} must be an array`);
  return value.map((pair, index) => {
    if (!Array.isArray(pair)) throw invalid(`${label}[${index}] must be an array`);
    return pair.map((item, itemIndex) => text(item, `${label}[${index}][${itemIndex}]`));
  });
}
function invalid(message: string): AnalysisValidationError {
  return new AnalysisValidationError(message);
}
