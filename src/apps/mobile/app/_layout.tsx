import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { AppProvider } from "../src/app-store";
import { OAuthSessionProvider } from "../src/oauth-session";

export default function RootLayout(): React.JSX.Element {
  return <AppProvider><OAuthSessionProvider><StatusBar style="dark" /><Stack screenOptions={{ headerShown: false, animation: "slide_from_right" }} /></OAuthSessionProvider></AppProvider>;
}
