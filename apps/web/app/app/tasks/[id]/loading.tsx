import { LoadingState } from "@/components/LoadingState";
import { TranslatedText } from "@/components/TranslatedText";

export default function AppTaskDetailsLoading() {
  return (
    <LoadingState
      title={<TranslatedText id="task.loading.title" />}
      description={<TranslatedText id="task.loading.details-description" />}
    />
  );
}
