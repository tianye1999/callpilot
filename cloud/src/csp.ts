export function liveKitConnectSources(value: string): string {
  try {
    const url = new URL(value);
    if (url.protocol !== "wss:" || !url.hostname || url.username || url.password) return "";
    return ` https://${url.host} wss://${url.host}`;
  } catch {
    return "";
  }
}
