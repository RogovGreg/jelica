"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { DocumentationHtmlViewer } from "../../../../packages/app-platform/src/documentation-ui";

export type DocumentationViewerRoute = {
  artifactSource: string;
  viewerHref: string;
};

type DocumentationViewerProps = {
  source: string;
  title: string;
  routes: readonly DocumentationViewerRoute[];
};

export function DocumentationViewer({ source, title, routes }: DocumentationViewerProps) {
  const router = useRouter();
  const frameRef = useRef<HTMLIFrameElement>(null);
  const removeFrameHashListener = useRef<(() => void) | null>(null);
  const [frameSource, setFrameSource] = useState(source);

  const synchronizeViewerRoute = useCallback(() => {
    const frameWindow = frameRef.current?.contentWindow;
    if (!frameWindow) {
      return;
    }

    try {
      const frameUrl = new URL(frameWindow.location.href);
      const target = routes.find(
        (route) => new URL(route.artifactSource, window.location.origin).pathname === frameUrl.pathname,
      );
      if (!target) {
        return;
      }

      const viewerUrl = `${target.viewerHref}${frameUrl.hash}`;
      if (`${window.location.pathname}${window.location.hash}` !== viewerUrl) {
        router.push(viewerUrl);
      }
    } catch {
      // The sandbox remains usable even if an unexpected artifact URL cannot be inspected.
    }
  }, [router, routes]);

  useEffect(() => {
    const synchronizeHash = () => {
      const artifactUrl = new URL(source, window.location.origin);
      artifactUrl.hash = window.location.hash;
      setFrameSource(`${artifactUrl.pathname}${artifactUrl.search}${artifactUrl.hash}`);
    };

    synchronizeHash();
    window.addEventListener("hashchange", synchronizeHash);
    return () => window.removeEventListener("hashchange", synchronizeHash);
  }, [source]);

  useEffect(
    () => () => {
      removeFrameHashListener.current?.();
    },
    [],
  );

  const handleFrameLoad = useCallback(() => {
    const frameWindow = frameRef.current?.contentWindow;
    removeFrameHashListener.current?.();
    synchronizeViewerRoute();
    if (frameWindow) {
      frameWindow.addEventListener("hashchange", synchronizeViewerRoute);
      removeFrameHashListener.current = () =>
        frameWindow.removeEventListener("hashchange", synchronizeViewerRoute);
    }
  }, [synchronizeViewerRoute]);

  return <DocumentationHtmlViewer frameRef={frameRef} source={frameSource} title={title} onLoad={handleFrameLoad} />;
}
