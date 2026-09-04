import { useCallback, useEffect, useRef, useState } from "react";

import { DocumentationHtmlViewer } from "../../../../packages/app-platform/src/documentation-ui";
import type { DocumentationBundle } from "../../../../packages/app-platform/src/documentation";
import type { DesktopDocumentationSelection } from "../common/contracts";
import type { DesktopPlatformAdapter } from "./platform";

type Route = Readonly<{ pageId: string; resourceId: string }>;

export function DesktopDocumentationViewer({ adapter, bundle, selection, source, anchor, title, onOpen }: {
  adapter: DesktopPlatformAdapter;
  bundle: DocumentationBundle;
  selection: DesktopDocumentationSelection;
  source: string;
  anchor: string;
  title: string;
  onOpen: (pageId: string, anchor?: string) => void;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const removeHashListener = useRef<(() => void) | null>(null);
  const [routes, setRoutes] = useState<readonly Route[]>([]);
  const [frameSource, setFrameSource] = useState(`${source}${anchor}`);

  useEffect(() => {
    let active = true;
    void Promise.all(bundle.manifest.pages.map(async (page) => ({ pageId: page.id, resourceId: (await adapter.resolveDocumentationPage(page.id, selection)).resourceId })))
      .then((items) => { if (active) setRoutes(items); })
      .catch(() => { if (active) setRoutes([]); });
    return () => { active = false; };
  }, [adapter, bundle, selection]);

  useEffect(() => { setFrameSource(`${source}${anchor}`); }, [anchor, source]);
  useEffect(() => () => removeHashListener.current?.(), []);

  const synchronizeRoute = useCallback(() => {
    const frameWindow = frameRef.current?.contentWindow;
    if (!frameWindow) return;
    try {
      const frameUrl = new URL(frameWindow.location.href);
      const target = routes.find((route) => new URL(route.resourceId).pathname === frameUrl.pathname);
      if (target) onOpen(target.pageId, frameUrl.hash);
    } catch {
      // The script-free sandbox remains usable if an external URL cannot be inspected.
    }
  }, [onOpen, routes]);

  const handleLoad = useCallback(() => {
    const frameWindow = frameRef.current?.contentWindow;
    removeHashListener.current?.();
    synchronizeRoute();
    if (frameWindow) {
      frameWindow.addEventListener("hashchange", synchronizeRoute);
      removeHashListener.current = () => frameWindow.removeEventListener("hashchange", synchronizeRoute);
    }
  }, [synchronizeRoute]);

  return <DocumentationHtmlViewer frameRef={frameRef} source={frameSource} title={title} onLoad={handleLoad} />;
}
