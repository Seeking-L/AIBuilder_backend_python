## 给 AI 的 Expo 基础说明（中英双语）

### 1. 什么是 Expo / What is Expo

- **中文**：Expo 是构建 React Native 跨平台应用的一套工具链和运行环境，它基于 React 组件开发模式，通过 Expo CLI 启动开发服务器、打包应用，并提供一系列预配置的原生能力（如相机、图片、推送等）。
- **English**: Expo is a toolchain and runtime for building cross‑platform apps with React Native. It uses React components, provides a dev server and bundler via Expo CLI, and offers many preconfigured native capabilities (camera, images, push notifications, etc.).

### 2. 项目结构和入口 / Project structure and entry point

- **中文**：
  - 本项目使用 **Expo Router**，它采用「约定式文件路由」：`app` 目录中的文件名决定页面路由。
  - `app/_layout.tsx` 是根布局，用于配置导航结构（例如一个 `Stack`）。
  - `app/index.tsx` 是应用入口页面，在当前固定骨架中只展示一个简单的 `Hello, world`。
- **English**:
  - This project uses **Expo Router**, which relies on file‑based routing: files inside the `app` directory define screens and routes.
  - `app/_layout.tsx` is the root layout where the main navigator (e.g. a `Stack`) is configured.
  - `app/index.tsx` is the main entry screen, which in this base template only renders a simple `Hello, world`.

### 3. 核心开发方式 / Core development model

- **中文**：
  - Expo 应用就是一个 React Native 应用：使用函数组件（Function Components）和 Hooks（如 `useState`, `useEffect`）。
  - UI 主要由 `react-native` 提供的基础组件（如 `View`, `Text`, `ScrollView`, `TextInput`, `Pressable`）以及项目内封装的通用组件（例如 `ScreenContainer`, `AppText`, `PrimaryButton`, `Spacer`）组合而成。
  - 样式通过 `StyleSheet.create` 定义，使用类似 CSS 的属性（如 `flex`, `padding`, `margin`, `color` 等）。
- **English**:
  - An Expo app is essentially a React Native app: it uses function components and hooks (e.g. `useState`, `useEffect`).
  - UI is built from basic `react-native` components (`View`, `Text`, `ScrollView`, `TextInput`, `Pressable`, etc.) combined with local common components such as `ScreenContainer`, `AppText`, `PrimaryButton`, and `Spacer`.
  - Styles are defined with `StyleSheet.create`, using CSS‑like properties such as `flex`, `padding`, `margin`, and `color`.

### 4. 导航与路由 / Navigation and routing

- **中文**：
  - 新页面通过在 `app` 目录下添加文件实现，例如：
    - `app/profile/index.tsx` → `/profile`
    - `app/posts/[id].tsx` → `/posts/:id`
  - 导航和路由由 Expo Router 负责，通常通过链接或 `router` API 进行页面跳转，而不是手动管理导航栈。
- **English**:
  - New screens are created by adding files under the `app` directory, for example:
    - `app/profile/index.tsx` → `/profile`
    - `app/posts/[id].tsx` → `/posts/:id`
  - Navigation and routing are handled by Expo Router, typically via link components or the `router` API, rather than manually managing a navigation stack.

### 5. 运行与二维码 / Running the app and QR code

- **中文**：
  - 在开发环境中，使用 `npm start` 或 `npx expo start` 启动 Expo 开发服务器。
  - 启动后，Expo 会在终端或网页界面显示一个二维码（或深链接），用户可以使用 Expo Go 应用扫码在真机上预览。
  - 在本项目场景中，后端会自动执行这些命令并把生成的二维码信息返回给前端。
- **English**:
  - In development, run `npm start` or `npx expo start` to launch the Expo dev server.
  - After it starts, Expo shows a QR code or deep link in the terminal or web UI; users can scan it with the Expo Go app to preview the app on a physical device.
  - In this project, the backend will automatically run these commands and send the generated QR code information to the frontend.

### 6. 给 AI 的关键约束 / Key constraints for the AI

- **中文**：
  - 只在允许的目录中修改或创建文件（例如 `app/`, `components/common/`, `hooks/`, `services/`, `types/`），不要更改工程配置文件（如 `package.json`, `tsconfig.json`）。
  - 新页面应遵守约定式路由命名，并优先复用通用 UI 组件，而不是随意引入新的第三方 UI 库。
- **English**:
  - Only create or edit files in the allowed directories (e.g. `app/`, `components/common/`, `hooks/`, `services/`, `types/`), and do not modify core project config files like `package.json` or `tsconfig.json`.
  - New screens should follow the file‑based routing conventions and prefer using existing common UI components instead of arbitrarily adding new third‑party UI libraries.
