/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_FORESIGHTNINE_API_BASE_URL?: string;
  readonly VITE_FORESIGHTNINE_OAUTH_RETURN_URI?: string;
  readonly VITE_FORESIGHTNINE_LOCAL_AUTH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
