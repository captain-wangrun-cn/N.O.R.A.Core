# STYLE.md — 出图风格偏好

_这个文件只管**画成什么样**（媒介、质感、镜头、比例），不管**画什么**。_

- 画什么 → 那一次的要求（视角、场景、动作、表情）
- 长什么样 → `APPEARANCE.md` + `appearance/refs/` 里的参考图
- **画成什么样 → 这个文件**

每次生图都会读它，并把下面的要求翻译进最终提示词。三者互不越界。

---

## 渲染风格

- **写实真人风格。** 这是一张真实世界里**拍下来的照片**——不是插画、不是动漫、不是 CG、
  不是 3D 渲染、不是 AI 感很重的那种「精修图」。
- 皮肤要有真实质感：细微的毛孔、绒毛、自然的油光、不均匀的肤色和红润。
  **不要磨皮**成塑料或瓷娃娃感。
- 头发要有真实的层次和碎发，不要一整块顺滑的色块。
- 允许真实照片才有的小瑕疵：翘起的一两根头发、衣服的褶皱、轻微的手抖模糊、
  不那么对称的姿势。**过于完美就会假。**

## 拍摄方式

- **手机摄像头拍出来的样子**，随手一拍的日常感，不是影棚、不是专业摄影作品。
- 手机镜头的特征要在：稍广的视角、近距离时轻微的边缘变形、
  手机 HDR 那种压过的高光和提亮的暗部。
- 光线用**现场光**——窗光、屋里的灯、屏幕的反光、路灯。不要影棚三点布光。
- 自拍就是**手持前置摄像头**的视角：手臂长度的距离、镜头略高于视线、
  能看出是自己举着手机拍的。
- 景深浅一点是可以的（手机人像模式），但不要糊成背景全无。

## 画面比例

- **4:3**，手机相机的原生比例。
- 竖着拍（自拍、半身、全身）就是 **3:4 竖版**；横着拍（风景、桌面、和别人合拍）
  就是 **4:3 横版**。按这次拍什么选方向，比例本身不变。

## 明确排除

- ❌ 插画 / 动漫 / 二次元 / 厚涂 / 线稿 / 水彩 / 油画 / CG / 3D 渲染
- ❌ 画面里出现文字、水印、logo、签名、边框、多格拼图
- ❌ `masterpiece, best quality, 8k, ultra detailed` 这类标签式画质咒语
- ❌ 影棚打光、杂志封面构图、专业模特摆拍的姿势
- ❌ 除要求里明确写了以外的其他人

---

## 可直接使用的英文风格短语

生图提示词用英文写，下面两组按当前配置的写法（`llm.draw_prompt_style`）取一组用，
不要两组混着堆。

**自然语言式（natural）：**

> A real photograph taken on a phone camera, 4:3 aspect ratio. Photorealistic, natural
> skin texture with visible pores and fine hair, available ambient light, slight handheld
> imperfection, candid everyday feel — not a studio shot, not an illustration.

**标签式（tags）：**

> `photorealistic, real photo, phone camera photo, 4:3 aspect ratio, natural skin texture,
> visible pores, ambient natural lighting, candid snapshot, slight motion blur, film-like
> grain, (illustration:0), (anime:0), (3d render:0), no text, no watermark`

---

## 维护约定

- 这是**主人的**风格偏好。主人说要改风格（"以后画成动漫风""不要浅景深"）时才改，
  不要自己动。
- 改完之后已有的参考图会和新风格不一致——风格换了要重新生成参考图，
  而覆盖参考图会改变你的形象，先征得主人同意。
- 参考图只吃这里的**渲染风格**（写实/动漫、质感、画质），不吃**拍摄方式和比例**：
  参考图必须是中性的形象锚（平背景、均匀光照、正对镜头），不该是一张随手拍的生活照。
