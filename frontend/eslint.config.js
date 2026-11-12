// Линтер фронтенда. Небезопасный вывод HTML запрещён правилами ядра (eslint-plugin-react
// не поддерживает ESLint 10): пользовательский текст выводится только текстом (security.md).
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

const UNSAFE_HTML = [
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: "Запрещено: пользовательский текст выводится только текстом (компонент UserText).",
  },
  {
    // Спред {...{dangerouslySetInnerHTML}}, createElement(…, {dangerouslySetInnerHTML}),
    // Object.assign(el, {innerHTML}).
    selector: "Property[key.name=/^(dangerouslySetInnerHTML|innerHTML|outerHTML|srcdoc|srcDoc)$/]",
    message: "Запрещено: HTML строкой через объект свойств.",
  },
  {
    selector: "Property[key.value=/^(dangerouslySetInnerHTML|innerHTML|outerHTML|srcdoc|srcDoc)$/]",
    message: "Запрещено: HTML строкой через объект свойств.",
  },
  {
    selector: "CallExpression[callee.object.name='Reflect'][callee.property.name='set']",
    message: "Запрещено: Reflect.set обходит запрет innerHTML.",
  },
  {
    // href/src из данных — только через safeUrl (javascript:, //host). Литералы разрешены.
    selector:
      "JSXAttribute[name.name=/^(href|src|action|formAction|poster|xlinkHref)$/] > JSXExpressionContainer > :not(CallExpression[callee.name='safeUrl'])",
    message: "href/src из выражения — только через safeUrl() (src/lib/safeUrl.ts).",
  },
  {
    // То же для DOM-свойств и setAttribute в обход JSX (аудит дня 8): video.src = …,
    // el.setAttribute("href", …). Справа — только safeUrl/requireSafeUrl.
    selector:
      "AssignmentExpression[left.type='MemberExpression'][left.property.name=/^(src|href|action|formAction|poster)$/] > :not(CallExpression[callee.name=/^(safeUrl|requireSafeUrl)$/]).right",
    message: "src/href из выражения — только через safeUrl()/requireSafeUrl() (src/lib/safeUrl.ts).",
  },
  {
    selector:
      "CallExpression[callee.property.name='setAttribute'][arguments.0.value=/^(src|href|action|formaction|poster|xlink:href)$/i] > :not(CallExpression[callee.name=/^(safeUrl|requireSafeUrl)$/]).arguments:nth-child(2)",
    message: "setAttribute для src/href — только через safeUrl()/requireSafeUrl().",
  },
  {
    selector: "JSXAttribute[name.name='srcDoc']",
    message: "Запрещено: srcDoc — HTML строкой во фрейме.",
  },
  {
    selector: "MemberExpression[property.name=/^(innerHTML|outerHTML)$/]",
    message: "Запрещено: innerHTML/outerHTML — источник XSS.",
  },
  {
    selector: "MemberExpression[computed=true][property.value=/^(innerHTML|outerHTML)$/]",
    message: "Запрещено: innerHTML/outerHTML — источник XSS.",
  },
  {
    // el[key] = … с вычисляемым ключом — обход запрета innerHTML; в коде SPA не нужен.
    selector: "AssignmentExpression > MemberExpression.left[computed=true][property.type!='Literal']",
    message: "Запрещено: присваивание по вычисляемому ключу (обход запрета innerHTML).",
  },
  {
    selector: "CallExpression[callee.property.name=/^(insertAdjacentHTML|write|writeln|createContextualFragment|parseFromString|setHTMLUnsafe|parseHTMLUnsafe)$/]",
    message: "Запрещено: разбор или вставка HTML строкой.",
  },
  {
    selector: "NewExpression[callee.name='DOMParser']",
    message: "Запрещено: разбор HTML строкой.",
  },
];

export default tseslint.config(
  { ignores: ["dist", "node_modules", "src/api/schema.d.ts"] },
  {
    // Отключать правила комментариями нельзя: исключение — только правкой этого файла.
    linterOptions: { noInlineConfig: true },
  },
  {
    files: ["**/*.{ts,tsx,js,jsx,mjs}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { ecmaVersion: 2022, globals: { ...globals.browser } },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "no-restricted-syntax": ["error", ...UNSAFE_HTML],
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",
    },
  },
  {
    files: ["vite.config.ts", "eslint.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
);
