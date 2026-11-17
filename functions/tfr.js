// Cloudflare Pages Function: GET /tfr
//
// Relays the FAA's list of active TFRs to the web app. The FAA server sends no CORS header,
// so a browser page cannot read it directly; this fetches it server-side and passes it on.
// It takes no input and forwards nothing about the visitor -- the request to the FAA is the
// same for everyone -- and the answer is cached for five minutes.
const SOURCE = "https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0" +
  "&request=GetFeature&typeName=TFR:V_TFR_LOC&outputFormat=application/json";

export async function onRequestGet() {
  let upstream;
  try {
    upstream = await fetch(SOURCE, {
      headers: { "User-Agent": "survey-flight-planner" },
      cf: { cacheTtl: 300, cacheEverything: true },
    });
  } catch (_) {
    return new Response("The FAA TFR server could not be reached.", { status: 502 });
  }
  if (!upstream.ok) {
    return new Response(`The FAA TFR server answered ${upstream.status}.`, { status: 502 });
  }
  return new Response(upstream.body, {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "public, max-age=300",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
