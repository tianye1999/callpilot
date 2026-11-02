type ClaimStage = "auth" | "offer_read" | "cas" | "edge_token" | "edge_send" | "phone_token" | "audit"
  | "permission" | "prepare_read" | "claim_finalize" | "cleanup_queue" | "claim_check" | "candidates";

export class ClaimTiming {
  private readonly started = performance.now();
  private readonly stages: Partial<Record<ClaimStage | "total", number>> = {};

  async measure<T>(stage: ClaimStage, operation: () => Promise<T>): Promise<T> {
    const started = performance.now();
    try {
      return await operation();
    } finally {
      this.stages[stage] = this.elapsed(started);
    }
  }

  finish(response: Response): Response {
    this.stages.total = this.elapsed(this.started);
    response.headers.set("Server-Timing", Object.entries(this.stages)
      .map(([stage, duration]) => `${stage};dur=${duration.toFixed(1)}`).join(", "));
    console.log(JSON.stringify(this.stages));
    return response;
  }

  private elapsed(started: number): number {
    return Math.max(0, Math.round((performance.now() - started) * 10) / 10);
  }
}
