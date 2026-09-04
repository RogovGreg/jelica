import { LoadingState } from "@/components/LoadingState";
import { TranslatedText } from "@/components/TranslatedText";

export default function AppTasksLoading() {
  return   <LoadingState title={<TranslatedText id="task.loading.list-title" />} description={<TranslatedText id="task.loading.list-description" />} />;
}
