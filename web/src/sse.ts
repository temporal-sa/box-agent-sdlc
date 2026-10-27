export interface ServerEvent { id?: string; event: string; data: string }

/** Incremental SSE parser: chunks may split lines, UTF-8, or event boundaries. */
export class SSEParser {
  private buffer = "";
  private data: string[] = [];
  private event = "message";
  private id: string | undefined;

  private readonly emit: (event: ServerEvent) => void;

  constructor(emit: (event: ServerEvent) => void) { this.emit = emit; }

  feed(chunk: string): void {
    this.buffer += chunk;
    let end: number;
    while ((end = this.buffer.indexOf("\n")) !== -1) {
      this.line(this.buffer.slice(0, end).replace(/\r$/, ""));
      this.buffer = this.buffer.slice(end + 1);
    }
  }

  private line(line: string): void {
    if (!line) {
      if (this.data.length) this.emit({ id: this.id, event: this.event, data: this.data.join("\n") });
      this.data = [];
      this.event = "message";
      this.id = undefined;
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "data") this.data.push(value);
    if (field === "event") this.event = value || "message";
    if (field === "id" && !value.includes("\0")) this.id = value;
  }
}
