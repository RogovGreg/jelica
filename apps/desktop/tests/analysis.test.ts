import assert from "node:assert/strict";
import test from "node:test";
import { serializeAnalysisOverrides, validateAnalysisOverrides, AnalysisValidationError } from "../../../packages/app-platform/src/analysis";

test("shared override serializer preserves false and empty lists and rejects unknown paths", () => {
  assert.deepEqual(serializeAnalysisOverrides({ statistics: { kmers: [], kmer_strand: "both" }, comparative_analysis: { enabled: false } }), ["--statistics.kmers=[]", "--statistics.kmer_strand=\"both\"", "--comparative_analysis.enabled=false"]);
  assert.throws(() => validateAnalysisOverrides({ unsafe: true }), AnalysisValidationError);
});
