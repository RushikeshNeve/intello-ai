import { Suspense } from "react";
import { PageLoading } from "@/features/shell/states";
import { ChatLoader } from "./chat-loader";

export default function NewChatPage({ params }: PageProps<"/o/[orgSlug]/chat">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <ChatLoader params={params} />
    </Suspense>
  );
}
