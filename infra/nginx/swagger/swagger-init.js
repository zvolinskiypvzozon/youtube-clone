// Инициализация Swagger UI отдельным файлом, а не встроенным скриптом: CSP /docs —
// script-src 'self' без 'unsafe-inline'. Параметры — как у FastAPI по умолчанию.
window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  layout: "BaseLayout",
  deepLinking: true,
  showExtensions: true,
  showCommonExtensions: true,
  presets: [SwaggerUIBundle.presets.apis],
});
