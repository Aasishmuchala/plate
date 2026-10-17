// Vitest setup for the canvas tests: jest-dom matchers, RTL cleanup, and the browser APIs jsdom lacks that
// xyflow needs to mount (ResizeObserver, DOMMatrixReadOnly, element sizes).

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

class DOMMatrixReadOnlyStub {
  m22 = 1;
  constructor(transform?: string) {
    const scale = typeof transform === "string" ? /scale\(([-\d.]+)/.exec(transform) : null;
    if (scale) this.m22 = Number(scale[1]);
  }
}

const g = globalThis as Record<string, unknown>;
if (!g.ResizeObserver) g.ResizeObserver = ResizeObserverStub;
if (!g.DOMMatrixReadOnly) g.DOMMatrixReadOnly = DOMMatrixReadOnlyStub;

HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect(): DOMRect {
  const rect = { x: 0, y: 0, top: 0, left: 0, width: 1280, height: 720, right: 1280, bottom: 720 };
  return { ...rect, toJSON: () => rect } as DOMRect;
};
