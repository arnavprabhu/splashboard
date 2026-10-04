import { Placeholder } from './Placeholder';

export default function ChatPage({ params }: { params?: { cid?: string } }) {
  return <Placeholder title="Chat." spec="§10.5" meta={params?.cid ? `Conversation ${params.cid}` : undefined} />;
}
