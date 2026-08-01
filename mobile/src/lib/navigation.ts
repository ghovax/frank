import { router } from "expo-router";

/**
 * Go back, or go home if there is no back.
 *
 * `router.back()` on an empty history does nothing and logs that `GO_BACK` was not handled,
 * which on a phone is a close button that does not close. It happens whenever a screen was
 * *entered* rather than pushed — a `frank://` pairing link, a notification, or the app being
 * reopened on the screen it was killed on — so every dismiss control needs this rather than the
 * bare call.
 */
export function goBack(): void {
  if (router.canGoBack()) {
    router.back();
    return;
  }
  router.replace("/");
}
