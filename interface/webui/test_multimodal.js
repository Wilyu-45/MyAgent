#!/usr/bin/env node
/* multimodal.js 离线冒烟 — Node 运行, 无依赖
 *   isValidDataUrl: 格式 / 大小 / 边界
 *   isSupportedFile / contentParts: 结构断言
 * 运行: node interface/webui/test_multimodal.js */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("  [PASS] " + name); }
  else { fail++; console.log("  [FAIL] " + name + (detail ? " — " + detail : "")); }
}

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "static", "multimodal.js"), "utf8"), sandbox);
const MM = sandbox.window.Multimodal;
check("暴露 window.Multimodal", !!MM);

const PNG = "data:image/png;base64," + Buffer.from("89504e470d0a1a0a", "hex").toString("base64");
const JPG = "data:image/jpeg;base64,/9j/4AAQSkZJRg==";

/* ---- isValidDataUrl: 格式 ---- */
check("合法 png data URL", MM.isValidDataUrl(PNG));
check("合法 jpeg data URL", MM.isValidDataUrl(JPG));
check("合法 gif data URL", MM.isValidDataUrl("data:image/gif;base64,R0lGODlhAQABAAAAACw="));
check("合法 webp data URL", MM.isValidDataUrl("data:image/webp;base64,UklGRg=="));
check("非 data URL 拒绝", !MM.isValidDataUrl("http://a/b.png"));
check("非图片 MIME 拒绝", !MM.isValidDataUrl("data:text/plain;base64,QUJD"));
check("base64 非法字符拒绝", !MM.isValidDataUrl("data:image/png;base64,!!!"));
check("空 base64 拒绝", !MM.isValidDataUrl("data:image/png;base64,"));
check("非字符串拒绝", !MM.isValidDataUrl(123) && !MM.isValidDataUrl(null) && !MM.isValidDataUrl(undefined));

/* ---- isValidDataUrl: 大小 (由 base64 长度推算, 上限 5MB) ---- */
const b64of = (bytes) => Buffer.from(bytes).toString("base64");
check("5MB 整放行",
      MM.isValidDataUrl("data:image/png;base64," + b64of(Buffer.alloc(5 * 1024 * 1024))));
check("超 5MB 拒绝",
      !MM.isValidDataUrl("data:image/png;base64," + b64of(Buffer.alloc(5 * 1024 * 1024 + 1))));
check("补位 (=) 推算不误判",
      MM.isValidDataUrl("data:image/png;base64," + b64of(Buffer.alloc(3))));

/* ---- isSupportedFile ---- */
check("png File 支持", MM.isSupportedFile({ type: "image/png" }));
check("jpg 别名支持", MM.isSupportedFile({ type: "image/jpg" }));
check("text File 拒绝", !MM.isSupportedFile({ type: "text/plain" }));
check("空对象拒绝", !MM.isSupportedFile({}) && !MM.isSupportedFile(null));

/* ---- contentParts ---- */
const parts = MM.contentParts("这是什么?", [PNG, JPG]);
check("parts: text 在前", parts.length === 3 && parts[0].type === "text" &&
      parts[0].text === "这是什么?");
check("parts: image_url 结构", parts[1].type === "image_url" &&
      parts[1].image_url.url === PNG && parts[2].image_url.url === JPG);
check("parts: 无图片退化为纯文本 part",
      MM.contentParts("hi", []).length === 1 && MM.contentParts("hi", [])[0].text === "hi");
check("上限常量 4 张 / 5MB", MM.MAX_IMAGES === 4 && MM.MAX_IMAGE_BYTES === 5 * 1024 * 1024);

/* ---- captureScreenshot: 截图直传 (离线桩环境) ---- */
(async () => {
  check("captureScreenshot 已暴露", typeof MM.captureScreenshot === "function");
  check("无 navigator → 优雅返回 null", (await MM.captureScreenshot()) === null);

  // 独立上下文加载同一份 multimodal.js, 通过改写全局注入桩 (函数读的是创建时上下文的全局)
  const SRC = fs.readFileSync(path.join(__dirname, "static", "multimodal.js"), "utf8");
  const shotCtx = { window: {} };
  vm.createContext(shotCtx);
  vm.runInContext(SRC, shotCtx);
  const MM2 = shotCtx.window.Multimodal;

  shotCtx.navigator = { mediaDevices: {} };
  check("mediaDevices 缺 getDisplayMedia → null", (await MM2.captureScreenshot()) === null);

  shotCtx.navigator = { mediaDevices: { getDisplayMedia: async () => { throw new Error("NotAllowedError"); } } };
  check("用户取消 (getDisplayMedia 拒绝) → null", (await MM2.captureScreenshot()) === null);

  // 桩 DOM: 抓帧 → canvas → data URL
  function fakeDom(shots) {
    let stopped = 0, call = 0;
    const stream = { getTracks: () => [{ stop: () => { stopped++; } }] };
    const video = { srcObject: null, muted: false, playsInline: false,
                    videoWidth: 1920, videoHeight: 1080,
                    play: async () => {}, onloadedmetadata: null };
    const canvas = { width: 0, height: 0,
                     getContext: () => ({ drawImage() {} }),
                     toDataURL: () => shots[Math.min(call++, shots.length - 1)] };
    shotCtx.document = { createElement: (t) => (t === "video" ? video : canvas) };
    shotCtx.requestAnimationFrame = (cb) => cb();
    shotCtx.navigator = { mediaDevices: { getDisplayMedia: async () => stream } };
    return { stopped: () => stopped, calls: () => call };
  }

  const ok = fakeDom([PNG]);
  const shot = await MM2.captureScreenshot();
  check("正常抓帧返回 data URL", shot === PNG);
  check("抓帧后释放媒体轨 (track.stop)", ok.stopped() === 1);

  const big = "data:image/png;base64," + b64of(Buffer.alloc(6 * 1024 * 1024)); // 超限
  const retry = fakeDom([big, PNG]);
  const shot2 = await MM2.captureScreenshot();
  check("首帧超限自动降采样重试", shot2 === PNG && retry.calls() === 2);

  const never = fakeDom([big]);
  check("持续超限重试 4 次后放弃 → null", (await MM2.captureScreenshot()) === null && never.calls() === 4);

  console.log("\n" + pass + " passed, " + fail + " failed");
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
