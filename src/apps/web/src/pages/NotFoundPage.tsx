import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <main className="center-page" data-page="NotFoundPage">
      <div>
        <h1>页面不存在</h1>
        <p className="meta">地址可能有误，或者页面已经移除。</p>
        <Link className="btn" to="/research">回到研究</Link>
      </div>
    </main>
  );
}
