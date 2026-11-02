type ClaimStage = "auth" | "offer_read" | "cas" | "edge_token" | "edge_send" | "phone_token" | "audit"
  | "permission" | "prepare_read" | "claim_finalize" | "cleanup_queue" | "claim_check" | "candidates"
  | "presence" | "prepare_batch" | "turn" | "sdp_relay" | "edge_reserve";

export class ClaimTiming {
  private readonly started = performance.now();
  private readonly stages: Partial<Record<ClaimStage | "total", number>> = {};
  private readonly starts: Partial<Record<`${"edge_reserve" | "turn" | "sdp_relay"}_start_ms`, number>> = {};
  private claimId?: string;

  tagClaim(claimId: string): void {
    if (/^claim_[A-Za-z0-9_-]{12,80}$/.test(claimId)) this.claimId = claimId;
  }

  async measure<T>(stage: ClaimStage, operation: () => Promise<T>): Promise<T> {
    const started = performance.now();
    if (stage === "edge_reserve" || stage === "turn" || stage === "sdp_relay") {
      this.starts[`${stage}_start_ms`] = this.elapsed(this.started);
    }
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
    console.log(JSON.stringify({ ...this.stages, ...this.starts, ...(this.claimId ? { claimId: this.claimId } : {}) }));
    return response;
  }

  private elapsed(started: number): number {
    return Math.max(0, Math.round((performance.now() - started) * 10) / 10);
  }
}
