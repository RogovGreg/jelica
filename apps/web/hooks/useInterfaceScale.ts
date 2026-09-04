"use client";

import { useEffect, useState } from "react";

import { getCurrentUser, updateCurrentUserPreferences } from "@/lib/api/client";
import {
  interfaceScaleRatio,
  INTERFACE_SCALE_STEPS,
  parseInterfaceScale,
  type InterfaceScale,
} from "../../../packages/app-platform/src/theme";

export const WEB_SCALE_STORAGE_KEY = "jelica-web-scale";
export const WEB_SCALE_COOKIE = "jelica-web-scale";

export function useInterfaceScale() {
  const [scale, setScaleState] = useState<InterfaceScale>(100);

  useEffect(() => {
    let active = true;
    const cached = parseInterfaceScale(window.localStorage.getItem(WEB_SCALE_STORAGE_KEY));
    applyInterfaceScale(cached);
    setScaleState(cached);

    const syncFromServer = () => {
      void getCurrentUser()
        .then((user) => {
          if (!active) return;
          applyInterfaceScale(user.interface_scale);
          window.localStorage.setItem(WEB_SCALE_STORAGE_KEY, String(user.interface_scale));
          setScaleState(user.interface_scale);
        })
        .catch(() => undefined);
    };

    syncFromServer();
    window.addEventListener("jelica-auth-changed", syncFromServer);
    return () => {
      active = false;
      window.removeEventListener("jelica-auth-changed", syncFromServer);
    };
  }, []);

  const setScale = (value: InterfaceScale) => {
    applyInterfaceScale(value);
    window.localStorage.setItem(WEB_SCALE_STORAGE_KEY, String(value));
    document.cookie = `${WEB_SCALE_COOKIE}=${value}; Path=/; Max-Age=31536000; SameSite=Lax`;
    setScaleState(value);
    void updateCurrentUserPreferences({ interface_scale: value })
      .then((user) => {
        applyInterfaceScale(user.interface_scale);
        window.localStorage.setItem(WEB_SCALE_STORAGE_KEY, String(user.interface_scale));
        document.cookie = `${WEB_SCALE_COOKIE}=${user.interface_scale}; Path=/; Max-Age=31536000; SameSite=Lax`;
        setScaleState(user.interface_scale);
      })
      .catch(() => undefined);
  };

  return { scale, setScale, options: INTERFACE_SCALE_STEPS };
}

function applyInterfaceScale(scale: InterfaceScale): void {
  document.documentElement.style.setProperty("--ui-scale", String(interfaceScaleRatio(scale)));
}
