import { z } from "zod";

const releaseIdentifier = z.string().min(1).max(64).regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/);

export const latestReleaseSchema = z.object({
  product: releaseIdentifier,
  platform: releaseIdentifier
}).strict();

export const releaseDownloadUrlSchema = z.url({ protocol: /^https$/ });
