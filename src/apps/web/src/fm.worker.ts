import { mountFmWorker, type FmWorkerPort } from "@foresightnine/fm-engine";

mountFmWorker(self as unknown as FmWorkerPort);
