import type { ComponentChildren } from "preact";
import { marked, type Token, type Tokens } from "marked";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import bash from "highlight.js/lib/languages/bash";
import { CodeBlock } from "../../components";
hljs.registerLanguage("python", python);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("bash", bash);
function safeLink(href: string): string | undefined {
  try {
    const u = new URL(href, location.origin);
    return ["http:", "https:", "mailto:"].includes(u.protocol)
      ? u.href
      : undefined;
  } catch {
    return undefined;
  }
}
function nodes(tokens: Token[]): ComponentChildren {
  return tokens.map((token, i) => {
    const children =
      "tokens" in token && Array.isArray(token.tokens)
        ? nodes(token.tokens)
        : "text" in token
          ? String(token.text)
          : "";
    switch (token.type) {
      case "space":
        return null;
      case "paragraph":
        return <p key={i}>{children}</p>;
      case "heading":
        return <h3 key={i}>{children}</h3>;
      case "strong":
        return <strong key={i}>{children}</strong>;
      case "em":
        return <em key={i}>{children}</em>;
      case "del":
        return <del key={i}>{children}</del>;
      case "codespan":
        return <code key={i}>{token.text}</code>;
      case "code": {
        const lang = (token.lang ?? "").split(/\s/)[0] ?? "";
        const html = hljs.getLanguage(lang)
          ? hljs.highlight(token.text, { language: lang }).value
          : null;
        return (
          <CodeBlock key={i} code={token.text} label={lang}>
            {html ? (
              <code dangerouslySetInnerHTML={{ __html: html }} />
            ) : (
              token.text
            )}
          </CodeBlock>
        );
      }
      case "link":
        return (
          <a
            key={i}
            href={safeLink(token.href)}
            target="_blank"
            rel="noopener noreferrer"
          >
            {children}
          </a>
        );
      case "image":
        return (
          <a
            key={i}
            href={safeLink(token.href)}
            target="_blank"
            rel="noopener noreferrer"
          >
            {token.text || "Image"}
          </a>
        );
      case "blockquote":
        return <blockquote key={i}>{children}</blockquote>;
      case "br":
        return <br key={i} />;
      case "hr":
        return <hr key={i} />;
      case "list":
        return token.ordered ? (
          <ol key={i}>
            {token.items.map((item: Tokens.ListItem, j: number) => (
              <li key={j}>{nodes(item.tokens)}</li>
            ))}
          </ol>
        ) : (
          <ul key={i}>
            {token.items.map((item: Tokens.ListItem, j: number) => (
              <li key={j}>{nodes(item.tokens)}</li>
            ))}
          </ul>
        );
      case "table":
        return (
          <div class="table-scroll" key={i}>
            <table>
              <thead>
                <tr>
                  {token.header.map((c: Tokens.TableCell, j: number) => (
                    <th key={j}>{nodes(c.tokens)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {token.rows.map((r: Tokens.TableCell[], j: number) => (
                  <tr key={j}>
                    {r.map((c: Tokens.TableCell, k: number) => (
                      <td key={k}>{nodes(c.tokens)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case "html":
        return <span key={i}>{token.raw}</span>;
      default:
        return <span key={i}>{children}</span>;
    }
  });
}
export function Markdown({ text }: { text: string }) {
  return <div class="markdown">{nodes(marked.lexer(text))}</div>;
}
