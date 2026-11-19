import { Suspense } from "react";
import { PageLoading } from "@/features/shell/states";
import { ChatLoader } from "../chat-loader";

export default function ThreadPage({ params }: PageProps<"/o/[orgSlug]/chat/[threadId]">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <ChatLoader params={params} />
    </Suspense>
  );
}
