/* 화면이 서버 JSON을 주고받을 때 공통으로 쓰는 읽기. */

/** 응답 본문을 JSON으로 읽는다. JSON이 아니면 앞부분 문장을 오류로 던진다. */
async function readJson(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (_error) {
    throw new Error(text.slice(0, 180) || "서버 응답을 해석하지 못했습니다");
  }
}

/** JSON을 POST하고 응답 객체와 본문을 함께 돌려준다. */
async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body == null ? undefined : JSON.stringify(body),
  });
  const data = await readJson(response);
  return { response, data };
}
