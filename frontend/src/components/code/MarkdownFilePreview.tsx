import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import "github-markdown-css/github-markdown.css";

/** Rendered preview of a Markdown file; its own chunk, loaded on first use. */
export default function MarkdownFilePreview({ source, theme }: { source: string; theme: "light" | "dark" }) {
  return (
    <div className="h-full overflow-auto bg-background px-4 py-4">
      <article
        className={`markdown-body mx-auto max-w-5xl ${
          theme === "dark" ? "markdown-body-dark" : ""
        }`}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeRaw, rehypeSanitize]}
        >
          {source}
        </ReactMarkdown>
      </article>
    </div>
  );
}
