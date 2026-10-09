// k6 load test: payment submissions through the Gateway, with status polling.
//   Stages: warm-up -> ramp -> sustained peak -> ramp down.
import http from "k6/http";
import { check, sleep } from "k6";
import { uuidv4 } from "https://jslib.k6.io/k6-utils/1.4.0/index.js";

const HOST = __ENV.HOST || "ledgerflow.localtest.me";
const BASE = `https://${HOST}`;
const API_KEY = __ENV.API_KEY;
const PEAK = parseInt(__ENV.PEAK_RPS || "300");

export const options = {
  insecureSkipTLSVerify: true, // local CA; the host is pinned via `hosts` below
  hosts: { [HOST]: __ENV.GATEWAY_IP },
  scenarios: {
    payments: {
      executor: "ramping-arrival-rate",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: 100,
      maxVUs: 600,
      stages: [
        { target: 20, duration: "30s" },
        { target: PEAK, duration: "2m" },
        { target: PEAK, duration: "3m" },
        { target: 10, duration: "1m" },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(99)<500"],
    checks: ["rate>0.99"],
  },
};

const params = {
  headers: { "X-API-Key": API_KEY, "Content-Type": "application/json" },
};
const ACCOUNTS = 20;
const account = (i) => `load-${i}`;

export function setup() {
  const mk = (id, negative) =>
    http.post(
      `${BASE}/v1/accounts`,
      JSON.stringify({ id, currency: "USD", allow_negative: negative }),
      params
    );
  mk("load-treasury", true);
  for (let i = 0; i < ACCOUNTS; i++) mk(account(i), false);
  // Seed every account so transfers between them do not bounce on insufficient funds.
  for (let i = 0; i < ACCOUNTS; i++) {
    http.post(
      `${BASE}/v1/transactions`,
      JSON.stringify({ from_account: "load-treasury", to_account: account(i), amount: 100000000, currency: "USD" }),
      { headers: { ...params.headers, "Idempotency-Key": uuidv4() } }
    );
  }
  sleep(5);
}

export default function () {
  const src = Math.floor(Math.random() * ACCOUNTS);
  const dst = (src + 1 + Math.floor(Math.random() * (ACCOUNTS - 1))) % ACCOUNTS;
  const res = http.post(
    `${BASE}/v1/transactions`,
    JSON.stringify({
      from_account: account(src),
      to_account: account(dst),
      amount: 1 + Math.floor(Math.random() * 500),
      currency: "USD",
    }),
    { headers: { ...params.headers, "Idempotency-Key": uuidv4() } }
  );
  check(res, { "accepted (202)": (r) => r.status === 202 });

  // 1 in 10 requests also reads the transaction back.
  if (res.status === 202 && Math.random() < 0.1) {
    const id = res.json("transaction_id");
    const get = http.get(`${BASE}/v1/transactions/${id}`, params);
    check(get, { "status readable": (r) => r.status === 200 });
  }
}
