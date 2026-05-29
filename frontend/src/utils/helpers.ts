export async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof data.detail === "string" ? data.detail : "Request failed";
    throw new Error(message);
  }
  return data as T;
}

export function renderHtml(html: string, fallback: string) {
  return { __html: html || fallback || "" };
}

export async function ensureMathJaxLoaded(): Promise<void> {
  if (typeof window === "undefined") return;
  if ((window as any).MathJax?.typesetPromise) return;

  if (!(window as any).__mathjaxLoadingPromise) {
    (window as any).MathJax = {
      tex: {
        inlineMath: [["\\(", "\\)"], ["$", "$"]],
        displayMath: [["\\[", "\\]"], ["$$", "$$"]]
      },
      svg: { fontCache: "global" }
    };

    (window as any).__mathjaxLoadingPromise = new Promise<void>((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js";
      script.async = true;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error("Failed to load MathJax"));
      document.head.appendChild(script);
    });
  }

  await (window as any).__mathjaxLoadingPromise;
}
