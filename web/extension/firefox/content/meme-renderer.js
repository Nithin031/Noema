(function (global) {
  "use strict";

  function text(svg, value, x, y, size, color) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("text-anchor", "middle");
    node.setAttribute("fill", color);
    node.setAttribute("font-family", "sans-serif");
    node.setAttribute("font-size", size);
    node.setAttribute("font-weight", "700");
    node.textContent = String(value || "");
    svg.appendChild(node);
  }

  function render(container, meme) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 900 500");
    svg.setAttribute("role", "img");
    const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    background.setAttribute("width", "900");
    background.setAttribute("height", "500");
    background.setAttribute("fill", "#1f2937");
    svg.appendChild(background);
    text(svg, meme && meme.top, "450", "190", "38", "white");
    text(svg, meme && meme.bottom, "450", "340", "34", "#fbbf24");
    container.appendChild(svg);
  }

  global.AIMemeRenderer = { render: render };
}(this));
