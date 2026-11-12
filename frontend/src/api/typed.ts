// Вызовы API, сверенные с OpenAPI (schema.d.ts, npm run gen:api) на этапе компиляции:
// метод и путь — только пара, объявленная в `paths`; параметры пути — ровно те, что в шаблоне;
// тело и ответ — типы из схемы. Неверный метод (POST вместо PUT у бана — ручная проверка
// дня 8) или опечатка в пути — ошибка `tsc` при сборке и в CI, а не 405 у пользователя.
import { request, type RequestOptions } from "./client";
import type { paths } from "./schema";

export type HttpMethod = "get" | "post" | "put" | "patch" | "delete";

/** Методы, которые схема объявляет у пути P (отсутствующие там — `?: never`). */
export type MethodsOf<P extends keyof paths> = {
  [M in HttpMethod]: paths[P][M] extends undefined ? never : M;
}[HttpMethod];

type Operation<P extends keyof paths, M extends MethodsOf<P>> = NonNullable<paths[P][M]>;

/** Имена параметров из шаблона пути: "/api/videos/{video_id}/access/{user_id}" → "video_id" | "user_id". */
export type PathParamNames<S extends string> = S extends `${string}{${infer K}}${infer Rest}` ? K | PathParamNames<Rest> : never;

type JsonOf<R> = R extends { content: { "application/json": infer J } } ? J : void;

/** Тип успешного ответа: 200, иначе 201, иначе 204 (тела нет). */
export type ResponseOf<P extends keyof paths, M extends MethodsOf<P>> =
  Operation<P, M> extends { responses: infer R }
    ? R extends { 200: infer Ok }
      ? JsonOf<Ok>
      : R extends { 201: infer Created }
        ? JsonOf<Created>
        : void
    : never;

/** Тело запроса из схемы; у операций без тела — never (передать нельзя). */
export type BodyOf<P extends keyof paths, M extends MethodsOf<P>> =
  Operation<P, M> extends { requestBody: { content: { "application/json": infer B } } } ? B : never;

type ParamsArg<P extends string> = [PathParamNames<P>] extends [never]
  ? { params?: undefined }
  : { params: Record<PathParamNames<P>, string> };

type BodyArg<P extends keyof paths, M extends MethodsOf<P>> = [BodyOf<P, M>] extends [never]
  ? { body?: undefined }
  : { body: BodyOf<P, M> };

export type ApiOptions<P extends keyof paths, M extends MethodsOf<P>> = ParamsArg<P & string> &
  BodyArg<P, M> & {
    query?: RequestOptions["query"];
    signal?: AbortSignal;
  };

/** Параметры пути или тело обязательны — тогда обязателен и аргумент options. */
type NeedsOptions<P extends keyof paths, M extends MethodsOf<P>> = [PathParamNames<P & string>] extends [never]
  ? [BodyOf<P, M>] extends [never]
    ? false
    : true
  : true;

/** Подстановка параметров в шаблон пути с кодированием каждого значения. */
export function buildPath(template: string, params: Record<string, string> | undefined): string {
  return template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const value = params?.[name];
    if (value === undefined) throw new Error(`Нет параметра пути: ${name}`);
    return encodeURIComponent(value);
  });
}

/**
 * Единственный способ обратиться к API из src/api/*.ts (ESLint запрещает прямой `request`).
 * Пример: api("put", "/api/admin/users/{user_id}/ban", { params: { user_id }, body: { reason } }).
 */
export function api<P extends keyof paths, M extends MethodsOf<P>>(
  method: M,
  path: P,
  ...args: NeedsOptions<P, M> extends true ? [options: ApiOptions<P, M>] : [options?: ApiOptions<P, M>]
): Promise<ResponseOf<P, M>>;
export function api(
  method: HttpMethod,
  path: string,
  options: { params?: Record<string, string>; body?: unknown; query?: RequestOptions["query"]; signal?: AbortSignal } = {},
): Promise<unknown> {
  return request(buildPath(path, options.params), {
    method: method.toUpperCase(),
    body: options.body,
    query: options.query,
    signal: options.signal,
  });
}

/** Ключ мока в тестах: "PUT /api/admin/users/u8/ban" — только метод и путь из схемы. */
type Concrete<S extends string> = S extends `${infer A}{${string}}${infer B}` ? `${A}${string}${Concrete<B>}` : S;
export type RouteKey = {
  [P in keyof paths & string]: { [M in MethodsOf<P>]: `${Uppercase<M>} ${Concrete<P>}` }[MethodsOf<P>];
}[keyof paths & string];
