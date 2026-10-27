// Trusted image-owned helper. Repository source is parsed, never executed here.
// This helper and its pinned dependencies run inside the credential-free sandbox.
import fs from 'node:fs/promises';
import path from 'node:path';
import { build } from 'esbuild';
import { parse, serialize } from 'parse5';

const ROOT = '/workspace';
const OUTPUT = path.join(ROOT, '.box-bundle-output.html');
const MAX_FILE = 256_000;
const MAX_HTML = 2_000_000;
const MIME = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.woff': 'font/woff',
  '.woff2': 'font/woff2', '.ttf': 'font/ttf' };

function fail(message) { throw new Error(message); }
function inside(value) { return value === ROOT || value.startsWith(ROOT + '/'); }
function attr(node, name) { return node.attrs?.find(item => item.name === name)?.value; }
function setAttr(node, name, value) {
  const found = node.attrs?.find(item => item.name === name);
  if (found) found.value = value;
  else (node.attrs ||= []).push({ name, value });
}
function text(node) { return (node.childNodes || []).map(child => child.value || '').join(''); }
function inlineText(node, value) {
  node.childNodes = [{ nodeName: '#text', value, parentNode: node }];
}
function remove(node) {
  node.parentNode.childNodes = node.parentNode.childNodes.filter(child => child !== node);
}
function escapeClose(value, tag) { return value.replace(new RegExp('</' + tag, 'gi'), '<\\/' + tag); }

async function localFile(reference, directory = ROOT) {
  if (!reference || /[?#\\\u0000]/.test(reference) || /^(?:[a-z][a-z\d+.-]*:|\/)/i.test(reference))
    fail('Only repository-local resource paths are supported');
  let decoded;
  try { decoded = decodeURIComponent(reference); } catch { fail('Invalid resource path encoding'); }
  const resolved = path.resolve(directory, decoded);
  if (!inside(resolved)) fail('Resource path escapes the snapshot');
  const real = await fs.realpath(resolved);
  if (!inside(real) || real !== resolved) fail('Symlink resources are not supported');
  const stat = await fs.stat(real);
  if (!stat.isFile() || stat.size > MAX_FILE) fail('Resource is missing or exceeds the file limit');
  return real;
}

async function readText(file) {
  const body = await fs.readFile(file);
  if (body.length > MAX_FILE) fail('Text resource exceeds the file limit');
  return new TextDecoder('utf-8', { fatal: true }).decode(body);
}

async function dataResource(reference, directory = ROOT) {
  if (reference.startsWith('data:')) {
    if (!/^data:(?:image\/(?:png|jpeg|gif|webp|svg\+xml|x-icon)|font\/(?:woff2?|ttf))[;,]/i.test(reference))
      fail('Unsupported embedded resource type');
    return reference;
  }
  const file = await localFile(reference, directory);
  const mime = MIME[path.extname(file).toLowerCase()];
  if (!mime) fail('Unsupported static resource type');
  return `data:${mime};base64,${(await fs.readFile(file)).toString('base64')}`;
}

const safeImports = {
  name: 'snapshot-only',
  setup(builder) {
    builder.onResolve({ filter: /.*/ }, async args => {
      if (args.pluginData?.resolved) return;
      if (args.path.startsWith('data:')) {
        if (args.kind !== 'url-token') fail('Embedded module imports are not supported');
        return { path: args.path, external: true };
      }
      if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(args.path)) fail('External module and stylesheet imports are not supported');
      if (!['url-token', 'import-rule'].includes(args.kind) && !args.path.startsWith('.') && !path.isAbsolute(args.path) && args.path !== 'orbit-money' && !args.path.startsWith('orbit-money/'))
        fail('Only the prepared orbit-money dependency is supported');
      const resolved = await builder.resolve(args.path, { resolveDir: args.resolveDir || ROOT, kind: args.kind,
        pluginData: { resolved: true } });
      if (resolved.errors.length) return { errors: resolved.errors };
      if (resolved.external || !inside(resolved.path)) fail('Import resolves outside the snapshot');
      const real = await fs.realpath(resolved.path);
      if (real !== resolved.path || !inside(real)) fail('Symlink imports are not supported');
      return { path: real };
    });
    builder.onLoad({ filter: /.*/ }, async args => {
      if (!inside(args.path)) fail('Builder cannot read files outside the snapshot');
      const stat = await fs.stat(args.path);
      if (!stat.isFile() || stat.size > MAX_FILE) fail('Imported file exceeds the bounded file limit');
      // The built-in loader performs parsing; no repository build configuration or plugins run.
      return;
    });
  },
};

const common = { absWorkingDir: ROOT, bundle: true, write: false, platform: 'browser', target: 'es2022',
  logLevel: 'silent', legalComments: 'none', charset: 'utf8', tsconfigRaw: {}, plugins: [safeImports],
  loader: Object.fromEntries(Object.keys(MIME).map(extension => [extension, 'dataurl'])) };

async function cssBundle(contents, directory, name) {
  const result = await build({ ...common, stdin: { contents, loader: 'css', resolveDir: directory, sourcefile: name } });
  if (result.outputFiles.length !== 1) fail('Stylesheet produced unsupported external assets');
  return escapeClose(result.outputFiles[0].text, 'style');
}

async function jsonFiles(directory = ROOT, prefix = '') {
  const result = {};
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    if (entry.name.startsWith('.') || ['node_modules', 'tests', 'test', 'vendor'].includes(entry.name)) continue;
    const relative = prefix + entry.name;
    if (entry.isSymbolicLink()) fail('Symlink snapshot entries are not supported');
    if (entry.isDirectory()) Object.assign(result, await jsonFiles(path.join(directory, entry.name), relative + '/'));
    else if (entry.name.endsWith('.json') && !['package.json', 'package-lock.json', 'tsconfig.json'].includes(entry.name)) {
      const body = await readText(path.join(directory, entry.name));
      JSON.parse(body);
      result[relative] = body;
    }
  }
  return result;
}

function fetchBootstrap(json) {
  return `(() => {
const assets = ${JSON.stringify(json)};
const base = new URL('.', document.baseURI);
Object.defineProperty(globalThis, 'fetch', { configurable: false, writable: false, value: async (input, init = {}) => {
  const request = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
  const url = new URL(request, document.baseURI);
  const method = String(init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
  if (method !== 'GET' || url.origin !== base.origin || !url.pathname.startsWith(base.pathname) || url.search)
    throw new TypeError('Network access is disabled in this self-contained storefront');
  const key = decodeURIComponent(url.pathname.slice(base.pathname.length));
  if (!Object.prototype.hasOwnProperty.call(assets, key)) throw new TypeError('JSON asset is not embedded: ' + key);
  return new Response(assets[key], { status: 200, headers: { 'Content-Type': 'application/json; charset=utf-8' } });
} });
try { Object.defineProperty(navigator, 'sendBeacon', { value: () => false }); } catch {}
})();`;
}

async function bundle() {
  const document = parse(await readText(await localFile('index.html')));
  const scripts = [];
  let body;
  async function visit(node) {
    if (node.tagName === 'body') body = node;
    if (['base', 'iframe', 'frame', 'object', 'embed'].includes(node.tagName)) fail('Unsupported embedded document or base URL');
    if (node.tagName === 'meta' && attr(node, 'http-equiv')) fail('HTTP-equiv metadata is not supported');
    if (node.tagName === 'script') {
      const type = (attr(node, 'type') || '').trim().toLowerCase();
      if (type === 'importmap') { remove(node); return; }
      if (['application/json', 'application/ld+json'].includes(type) && !attr(node, 'src')) return;
      if (type !== 'module' || attr(node, 'async') !== undefined) fail('Only ordered module script entries are supported');
      const source = attr(node, 'src');
      if (source) {
        if (text(node).trim()) fail('Module entries cannot combine src and inline code');
        scripts.push(await localFile(source));
      } else {
        const file = path.join(ROOT, `.box-inline-${scripts.length}.mjs`);
        await fs.writeFile(file, text(node), { mode: 0o600 });
        scripts.push(file);
      }
      remove(node);
      return;
    }
    if (node.tagName === 'link') {
      const rel = (attr(node, 'rel') || '').toLowerCase();
      if (rel === 'stylesheet') {
        const file = await localFile(attr(node, 'href'));
        node.tagName = node.nodeName = 'style';
        node.attrs = [];
        inlineText(node, await cssBundle(await readText(file), path.dirname(file), path.basename(file)));
      } else if (rel === 'icon') setAttr(node, 'href', await dataResource(attr(node, 'href')));
      else { remove(node); return; }
    } else if (node.tagName === 'style') inlineText(node, await cssBundle(text(node), ROOT, 'inline.css'));
    for (const item of node.attrs || []) {
      if (item.name === 'srcset') fail('Responsive external asset sets are not supported');
      if (['src', 'poster'].includes(item.name)) item.value = await dataResource(item.value);
      if (item.name === 'href' && node.tagName !== 'link' && !item.value.startsWith('#')) fail('Only in-page links are supported');
      if (item.name === 'action' || item.name === 'formaction') fail('Network form submissions are not supported');
      if (item.name === 'style' && /url\s*\(|@import/i.test(item.value)) fail('Resource URLs in style attributes are not supported');
      if (item.name === 'xlink:href' && !item.value.startsWith('#')) fail('External SVG resources are not supported');
    }
    for (const child of [...(node.childNodes || [])]) await visit(child);
    if (node.content) await visit(node.content);
  }
  await visit(document);
  if (!body) fail('Storefront HTML has no body');
  let javascript = '';
  if (scripts.length) {
    const result = await build({ ...common, format: 'iife', stdin: {
      contents: scripts.map(file => `import ${JSON.stringify(file)};`).join('\n'), loader: 'js', resolveDir: ROOT,
      sourcefile: 'box-storefront-entry.mjs' } });
    if (result.outputFiles.length !== 1) fail('Modules produced unsupported external assets');
    javascript = result.outputFiles[0].text;
  }
  const script = { nodeName: 'script', tagName: 'script', attrs: [], namespaceURI: 'http://www.w3.org/1999/xhtml',
    parentNode: body, childNodes: [] };
  inlineText(script, escapeClose(fetchBootstrap(await jsonFiles()) + '\n' + javascript, 'script'));
  body.childNodes.push(script);
  const html = serialize(document);
  if (Buffer.byteLength(html, 'utf8') > MAX_HTML) fail('Self-contained storefront exceeds the 2 MB limit');
  await fs.writeFile(OUTPUT, html, { mode: 0o600 });
  console.log('Self-contained storefront built; no runtime resource requests required');
}

if (process.argv.includes('--self-test')) console.log('box-bundler ready');
else bundle().catch(error => { console.error(`Storefront bundle rejected: ${error.message}`); process.exitCode = 1; });
