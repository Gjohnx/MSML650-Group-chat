// Cognito sign-up and sign-in without an SDK. Owner: role 5b.
//
// The user pool API takes plain JSON over HTTPS, so fetch is enough. The app
// client allows USER_PASSWORD_AUTH and has no secret (infra/stack.py).

const SESSION_KEY = "groupchat.session";
let cfg = null;

export function initAuth(config) {
  cfg = config;
}

async function cognito(target, body) {
  const res = await fetch(`https://cognito-idp.${cfg.region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || data.Message || `${target} failed (${res.status})`);
    err.code = (data.__type || "").split("#").pop();
    throw err;
  }
  return data;
}

export function decodeJwt(token) {
  const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  const json = decodeURIComponent(
    [...atob(part)].map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join(""),
  );
  return JSON.parse(json);
}

function load() {
  try {
    return JSON.parse(localStorage.getItem(SESSION_KEY)) || null;
  } catch {
    return null;
  }
}

function save(session) {
  try {
    if (session) localStorage.setItem(SESSION_KEY, JSON.stringify(session));
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    // Storage blocked: the session just won't survive a reload.
  }
  current = session;
}

let current = load();

function sessionFrom(result, refreshToken) {
  const claims = decodeJwt(result.IdToken);
  return {
    idToken: result.IdToken,
    refreshToken: result.RefreshToken || refreshToken,
    expiresAt: claims.exp * 1000,
    userId: claims.sub,
    username: claims["cognito:username"],
  };
}

export function signUp(username, email, password) {
  return cognito("SignUp", {
    ClientId: cfg.appClientId,
    Username: username,
    Password: password,
    UserAttributes: [{ Name: "email", Value: email }],
  });
}

export function confirmSignUp(username, code) {
  return cognito("ConfirmSignUp", { ClientId: cfg.appClientId, Username: username, ConfirmationCode: code });
}

export function resendCode(username) {
  return cognito("ResendConfirmationCode", { ClientId: cfg.appClientId, Username: username });
}

export async function signIn(username, password) {
  const r = await cognito("InitiateAuth", {
    ClientId: cfg.appClientId,
    AuthFlow: "USER_PASSWORD_AUTH",
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  if (r.ChallengeName) throw new Error(`Sign-in needs ${r.ChallengeName}, which this client does not handle`);
  save(sessionFrom(r.AuthenticationResult));
  return current;
}

export function signOut() {
  save(null);
}

export function currentUser() {
  return current ? { userId: current.userId, username: current.username } : null;
}

// Returns an ID token good for at least another minute, refreshing if needed.
// Null means the user has to sign in again.
export async function idToken() {
  if (!current) return null;
  if (current.expiresAt - Date.now() > 60_000) return current.idToken;
  if (!current.refreshToken) return null;
  try {
    const r = await cognito("InitiateAuth", {
      ClientId: cfg.appClientId,
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: current.refreshToken },
    });
    save(sessionFrom(r.AuthenticationResult, current.refreshToken));
    return current.idToken;
  } catch {
    save(null);
    return null;
  }
}
