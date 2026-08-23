import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { AppProvider } from "../src/app-store";

export default function RootLayout(): React.JSX.Element {
  return <AppProvider><StatusBar style="dark" /><Stack screenOptions={{ headerShown: false, animation: "slide_from_right" }} /></AppProvider>;
}
