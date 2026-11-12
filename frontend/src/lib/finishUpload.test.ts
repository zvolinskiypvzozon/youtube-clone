import { afterEach, describe, expect, it, vi } from "vitest";
import { json, mockApi } from "../test/utils";
import { COMPLETE_ATTEMPTS, finishUpload } from "./finishUpload";

const PARTS = [{ number: 1, etag: '"e1"' }];
const noSleep = () => Promise.resolve();
const card = (status: string) => ({ id: "v9", title: "t", state: { status } });

afterEach(() => vi.unstubAllGlobals());

function api(complete: () => Response | object, status = "uploading") {
  const completeMock = vi.fn(complete);
  const cardMock = vi.fn(() => card(status));
  mockApi({ "POST /api/uploads/v9/complete": completeMock, "GET /api/videos/v9": cardMock });
  return { completeMock, cardMock };
}

describe("finishUpload", () => {
  it("успех с первой попытки — без проверки карточки", async () => {
    const { completeMock, cardMock } = api(() => ({ video_id: "v9", status: "processing" }));
    await expect(finishUpload("v9", PARTS, noSleep)).resolves.toBeUndefined();
    expect(completeMock).toHaveBeenCalledTimes(1);
    expect(cardMock).not.toHaveBeenCalled();
  });

  it("временная ошибка (503, 429, сеть) — повтор, затем успех", async () => {
    let n = 0;
    const responses = [json({ detail: "x" }, 503), json({ detail: "x" }, 429)];
    const { completeMock } = api(() => responses[n++] ?? { video_id: "v9", status: "processing" });
    await finishUpload("v9", PARTS, noSleep);
    expect(completeMock).toHaveBeenCalledTimes(3);
  });

  it("сеть оборвалась — повтор", async () => {
    let n = 0;
    const fetchMock = vi.fn(async () => {
      if (n++ === 0) throw new TypeError("Failed to fetch");
      return json({ video_id: "v9", status: "processing" });
    });
    vi.stubGlobal("fetch", fetchMock);
    await finishUpload("v9", PARTS, noSleep);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("409 (прошлый complete успел) и видео в обработке — успех", async () => {
    const { cardMock } = api(() => json({ detail: "Загрузка уже завершена" }, 409), "processing");
    await expect(finishUpload("v9", PARTS, noSleep)).resolves.toBeUndefined();
    expect(cardMock).toHaveBeenCalledTimes(1);
  });

  it("409 и видео не в обработке (failed) — ошибка", async () => {
    api(() => json({ detail: "Загрузка уже завершена" }, 409), "failed");
    await expect(finishUpload("v9", PARTS, noSleep)).rejects.toMatchObject({ status: 409 });
  });

  it("400 (части не совпали) — сразу ошибка, без повторов и проверки", async () => {
    const { completeMock, cardMock } = api(() => json({ detail: "Не все части загружены" }, 400));
    await expect(finishUpload("v9", PARTS, noSleep)).rejects.toMatchObject({ status: 400 });
    expect(completeMock).toHaveBeenCalledTimes(1);
    expect(cardMock).not.toHaveBeenCalled();
  });

  it(`${COMPLETE_ATTEMPTS} временных ошибок: видео в обработке — успех; иначе ошибка`, async () => {
    const ok = api(() => json({ detail: "x" }, 502), "processing");
    await expect(finishUpload("v9", PARTS, noSleep)).resolves.toBeUndefined();
    expect(ok.completeMock).toHaveBeenCalledTimes(COMPLETE_ATTEMPTS);
    vi.unstubAllGlobals();
    api(() => json({ detail: "x" }, 502), "uploading");
    await expect(finishUpload("v9", PARTS, noSleep)).rejects.toMatchObject({ status: 502 });
  });
});
