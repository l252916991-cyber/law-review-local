import { forwardApi } from "@/lib/proxy";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export { forwardApi as GET, forwardApi as POST, forwardApi as PUT, forwardApi as PATCH, forwardApi as DELETE, forwardApi as HEAD, forwardApi as OPTIONS };
