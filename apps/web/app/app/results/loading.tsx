import { LoadingState } from "@/components/LoadingState";
import { TranslatedText } from "@/components/TranslatedText";

export default function AppResultsLoading() {
  return <LoadingState title={<TranslatedText id="result.loading.title" />} description={<TranslatedText id="result.loading.description" />} />;
}
