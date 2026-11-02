// Local workerd test entry only. Production entry/config never import this file.
import worker, { EdgeRoom } from "../src/index";

const networkFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (url === "https://rtc.live.cloudflare.com/v1/turn/keys/synthetic-direct-key/credentials/generate-ice-servers") {
    // Closed TCP fails promptly; localhost host candidates still exercise DTLS/SRTP.
    return Promise.resolve(Response.json({ iceServers: [{
      urls: ["turn:127.0.0.1:9?transport=tcp"], username: "synthetic", credential: "synthetic"
    }] }));
  }
  return networkFetch(input, init);
};

export { EdgeRoom };
export default worker;
