import { ReactFlowProvider } from "@xyflow/react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import App from "./App";
import { createInitialGraph, useGraphStore } from "./store";

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
});

const mount = () =>
  render(
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>,
  );

describe("App", () => {
  it("renders the placeholder canvas without throwing and lists Plate, Comp, Gate", () => {
    mount();
    const items = screen.getAllByTestId("node-item").map((li) => li.textContent);
    expect(items).toEqual(["Plate", "Comp", "Gate"]);
  });

  it("shows the Make bar as a pending quote, never a price", () => {
    mount();
    const bar = screen.getByTestId("make-bar");
    expect(bar).toHaveTextContent("provider");
    expect(bar).toHaveTextContent("custody");
    expect(bar).toHaveTextContent("quote pending");
  });

  it("shows the last refusal in the node list", () => {
    useGraphStore.setState({ lastRefusal: "cycle" });
    mount();
    expect(screen.getByTestId("last-refusal")).toHaveTextContent("cycle");
  });
});
