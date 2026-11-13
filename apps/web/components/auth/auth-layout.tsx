"use client";

import type { ReactNode } from "react";
import Icon, { type IconName } from "@/components/ui/icon";
import BrandMark from "@/components/ui/brand-mark";
import { useI18n } from "@/i18n/provider";

const COPY = {
  "zh-CN": {
    title: "把智能体，带入工作。",
    description: "连接知识与工具，让每一次执行都有上下文、有边界、有迹可循。",
    note: "一个工作区，贯通构建、执行与评估。",
    workflow: "从知识到行动",
    steps: ["连接知识", "配置智能体", "受控执行", "回看与评估"],
    caption: "知识、工具与人工决策，在同一条工作流中协作。",
    welcome: "你的智能体工作空间",
    login: "登录后继续处理工作，或创建一个新账号。",
    register: "创建账号，开始组织你的智能体与工作流。",
  },
  "en-US": {
    title: "Put your agents to work.",
    description: "Connect knowledge and tools. Give every action the context, boundaries, and evidence it needs.",
    note: "One workspace to build, run, and evaluate.",
    workflow: "From knowledge to action",
    steps: ["Connect knowledge", "Configure agents", "Govern actions", "Review outcomes"],
    caption: "Knowledge, tools, and human decisions, working together.",
    welcome: "Your agent workspace",
    login: "Sign in to pick up your work, or create a new account.",
    register: "Create an account to organize your agents and workflows.",
  },
};

const STEP_ICONS: IconName[] = ["knowledge", "agents", "approvals", "evaluations"];

export default function AuthLayout({ children, mode }: { children: ReactNode; mode: "login" | "register" }) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  return (
    <div className="auth-layout">
      <aside className="auth-story">
        <div className="auth-story-brand" translate="no">
          <BrandMark size={34} />
          <span>AgentHub</span>
        </div>
        <div className="auth-story-copy">
          <h2>{copy.title}</h2>
          <p>{copy.description}</p>
        </div>
        <div className="auth-flow" aria-label={copy.workflow}>
          <div className="auth-flow-heading"><span className="auth-flow-indicator" aria-hidden="true" />{copy.workflow}</div>
          <ol className="auth-flow-steps">
            {copy.steps.map((step, index) => (
              <li key={step}>
                <span className="auth-flow-icon"><Icon name={STEP_ICONS[index]} size={21} /></span>
                <span>{step}</span>
                {index < copy.steps.length - 1 && <span className="auth-flow-line" aria-hidden="true" />}
              </li>
            ))}
          </ol>
          <p>{copy.caption}</p>
        </div>
        <p className="auth-story-note">{copy.note}</p>
      </aside>
      <section className="auth-form-side" aria-labelledby="auth-form-title">
        <div className="auth-card">
          <div className="auth-form-intro">
            <span className="auth-mobile-brand" translate="no">AgentHub</span>
            <p>{copy.welcome}</p>
          </div>
          {children}
          <p className="auth-form-note">{copy[mode]}</p>
        </div>
      </section>
    </div>
  );
}
