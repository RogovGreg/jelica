import { LoadingState } from "@/components/LoadingState";
import { TranslatedText } from "@/components/TranslatedText";

export default function AppResultDetailsLoading() {
  return <LoadingState title={<TranslatedText id="result.loading.details-title" />} description={<TranslatedText id="result.loading.details-description" />} />;
}
