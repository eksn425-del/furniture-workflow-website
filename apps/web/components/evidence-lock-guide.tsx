const GATES = [
  {
    number: "A",
    gate: "资产闸门",
    requirement: "身份与 URL 角色完整",
    evidence: "record_id · 来源 URL · SHA-256 · 资产角色",
  },
  {
    number: "B",
    gate: "尺寸闸门",
    requirement: "完整 W / D / H 选项可追溯且不混轴",
    evidence: "工程图或结构化数据 · 最小完整选项 · 资产哈希",
  },
  {
    number: "C",
    gate: "双 AI 审查",
    requirement: "独立读数通过 smallest-complete-option.v1",
    evidence: "AI A / AI B · W×D×H 排序 · 轴差值 · 策略版本",
  },
  {
    number: "D",
    gate: "目录锁",
    requirement: "标准表 name 保持不变，导出时品牌只加一次",
    evidence: "name / size / image_url · 品牌规则 · 记录集哈希",
  },
];

export function EvidenceLockGuide() {
  return (
    <section className="lock-section" aria-labelledby="lock-title">
      <div className="section-kicker">
        <span>CATALOG CONTROL</span>
          <span>AUTO LOCK POLICY / 04</span>
      </div>

      <div className="lock-heading">
        <div>
          <p className="eyebrow">证据与锁表</p>
          <h2 id="lock-title">可追溯，才可交付</h2>
        </div>
        <p>
          以下是系统契约说明，不代表任何项目已经通过。项目结果必须由 API
          返回的证据、决议和目录锁记录证明。
        </p>
      </div>

      <div className="lock-grid">
        <div className="table-shell">
          <table>
            <caption className="sr-only">目录锁定前的四项证据闸门</caption>
            <thead>
              <tr>
                <th scope="col">编号</th>
                <th scope="col">闸门</th>
                <th scope="col">必须成立</th>
                <th scope="col">审计凭据</th>
              </tr>
            </thead>
            <tbody>
              {GATES.map((gate) => (
                <tr key={gate.number}>
                  <td>
                    <span className="gate-number">{gate.number}</span>
                  </td>
                  <th scope="row">{gate.gate}</th>
                  <td>{gate.requirement}</td>
                  <td>{gate.evidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <aside className="rule-card" aria-label="锁表三条硬规则">
          <span className="stamp">NO ESTIMATE</span>
          <h3>三条硬规则</h3>
          <ol>
            <li>
              <span>01</span>
              缺少任一尺寸轴的官方证据，不得估算补齐。
            </li>
            <li>
              <span>02</span>
              两次 AI 结果都只是候选；只有确定性策略能够写入 verified。
            </li>
            <li>
              <span>03</span>
              多个完整选项取 W×D×H 最小的一组；禁止跨选项混轴。双方不一致时自动重试或阻断。
            </li>
          </ol>
        </aside>
      </div>
    </section>
  );
}
