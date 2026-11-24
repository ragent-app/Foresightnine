import { describe, expect, it } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import { shortQuestion, topicTitle } from "./research";

const topic = (question: string, title: string | null = null) =>
  ({ title, request: { question, source_ids: [] }, versions: [] }) as unknown as ResearchV2.ResearchTopic;

describe("topic title", () => {
  it("uses the summarised title, else the question's opening", () => {
    expect(topicTitle(topic("收集一下最近半天发生的NVIDIA重磅事件有关的新闻信息和金融信息", "英伟达近期重磅事件"))).toBe("英伟达近期重磅事件");
    expect(topicTitle(topic("营收增长多快？"))).toBe("营收增长多快？");
    expect(shortQuestion("收集一下最近半天发生的NVIDIA重磅事件有关的新闻信息和金融信息")).toBe("收集一下最近半天发生的NVIDIA重磅事件有关的…");
    expect(shortQuestion("英伟达今天为什么大涨？请结合公告和行情分析，并列出主要风险")).toBe("英伟达今天为什么大涨");
  });
});
