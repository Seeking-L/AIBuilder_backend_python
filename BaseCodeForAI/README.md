## BaseCodeForAI 约定说明（Expo 固定骨架）

本目录下的 `baseExpo` 是一个**极简且固定的 Expo 应用骨架**，后端 AI 在此基础上生成页面和业务逻辑。所有自动构建的应用都必须遵守以下约定。

### 一、目录结构约定

- **`baseExpo/app/`**：页面和路由文件
  - 仅允许在此目录下新增页面文件（如 `app/profile/index.tsx`）。
  - 根布局文件固定为：`app/_layout.tsx`。
  - 根入口页面固定为：`app/index.tsx`。
- **`baseExpo/components/common/`**：通用 UI 组件（无业务）
  - 例如：`ScreenContainer.tsx`、`AppText.tsx`、`PrimaryButton.tsx`、`Spacer.tsx`。
- **`baseExpo/hooks/`**：通用 hooks
  - 如：状态管理、工具类 hooks，可供多个页面复用。
- **`baseExpo/services/`**：数据与网络访问
  - 必须通过这里暴露统一的 API 函数（如 `request<T>(...)`、`getUserList()`）。
- **`baseExpo/types/`**：共享类型定义
  - 如接口返回类型、业务实体类型等。

### 二、AI 可操作范围与禁止事项

- **允许 AI 创建/修改的目录**：
  - `baseExpo/app/`
  - `baseExpo/components/common/`
  - `baseExpo/hooks/`
  - `baseExpo/services/`
  - `baseExpo/types/`
- **禁止 AI 修改的内容**：
  - `baseExpo/package.json`
  - `baseExpo/tsconfig.json`
  - `baseExpo` 下的 ESLint / 配置文件
  - 任何构建脚本和重置脚本（如 `scripts/reset-project.js`）
- **路由命名规则**：
  - 所有页面必须使用 Expo Router 约定式路由，例如：
    - `app/index.tsx`（根页面）
    - `app/profile/index.tsx`
    - `app/settings/index.tsx`
    - `app/posts/[id].tsx`

### 三、页面与组件使用约定（概要）

- 所有页面应使用函数组件 + TypeScript。
- 页面外层应优先使用通用组件（在 `components/common` 中定义）进行布局和样式控制。
- 网络请求或数据访问必须封装在 `services/` 中，页面组件内不直接使用 `fetch`。

### 四、通用 UI 组件使用示例

下面是一个示例页面，展示如何使用通用组件组合出一个简单界面：

```tsx
// app/example.tsx
import { ScreenContainer } from '../components/common/ScreenContainer';
import { AppText } from '../components/common/AppText';
import { PrimaryButton } from '../components/common/PrimaryButton';
import { Spacer } from '../components/common/Spacer';

export default function ExampleScreen() {
  return (
    <ScreenContainer>
      <AppText variant="title">示例页面</AppText>
      <AppText>这是一个使用通用组件构建的简单页面。</AppText>
      <Spacer size={16} />
      <PrimaryButton label="点击我" onPress={() => {}} />
    </ScreenContainer>
  );
}
```

- **约定**：
  - 页面最外层使用 `ScreenContainer` 包裹。
  - 所有标题和正文文本使用 `AppText`，并通过 `variant` 区分样式。
  - 主操作按钮使用 `PrimaryButton`。
  - 元素之间的间距使用 `Spacer` 控制。

