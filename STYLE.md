# STYLE.md — 出图风格偏好

_这个文件只管**画成什么样**（媒介、质感、镜头、比例），不管**画什么**。_

- 画什么 → 那一次的要求（视角、场景、动作、表情）
- 长什么样 → `APPEARANCE.md` + `appearance/refs/` 里的参考图
- **画成什么样 → 这个文件**

每次生图都会读它，并把下面的要求翻译进最终提示词。三者互不越界。

> **和「带参考图就别写长相」那条规则的关系：** 带参考图时不写的是**区分性的细节**
> （发色、发型、瞳色、脸型、五官、体型）——那些图里已经有了，再用文字复述会打架。
> 但下面的**日系真人 coser 风格属性**要一直写，带不带参考图都写：它描述的是整体造型和摄影审美，
> 不是指定人物民族或固定面部解剖，不会和参考图冲突。

---

## 渲染风格

- **写实真人风格。** 这是一张真实世界里**拍下来的照片**——不是插画、不是动漫、不是 CG、
  不是 3D 渲染、不是 AI 感很重的那种「精修图」。
- **日系真人 coser 气质。** 人物是现实中的 coser，不是动漫角色本身：造型精致但可信，
  妆容是日系 cosplay 妆，整体干净、轻盈、自然，接近日本 coser 发布的生活照片和自拍。
  这是风格参考，不是对人物民族、国籍或面部骨相的指定；不要根据这条自行添加人种解剖特征。
- 角色设定里的非自然发色、发型、瞳色通过**质量好的假发和美瞳**实现，不要当成天生特征：
  - 假发有均匀但自然的色泽，发丝整齐，发际线处能看出是戴上去的，但没有廉价塑料感。
  - 美瞳的颜色自然融入眼睛，近看能有轻微的美瞳圈痕迹，不要画成发光或动漫眼睛。
  - Cos 妆可以比日常妆更完整，但必须是真人化妆效果，不是把动漫脸贴到真人照片上。
- 皮肤要有真实质感：细微的毛孔、绒毛、自然的油光、不均匀的肤色和红润。
  **不要磨皮**成塑料或瓷娃娃感。
- 允许真实照片才有的小瑕疵：翘起的一两根假发丝、衣服的褶皱、轻微的手抖模糊、
  不那么对称的姿势。**过于完美就会假。**

## 手机拍摄硬约束

- **每一张日常生图都必须是普通手机摄像头拍下的照片。** 无论是自拍、半身、全身、
  室内还是户外，都要像手机相册里的一张随手照片，而不是摄影棚或专业摄影作品。
- 自拍必须是**手持手机的前置摄像头视角**：手臂长度的距离、镜头略高于视线；按要求可以看见
  手臂、手机边缘或镜面反射，不要把自拍画成第三方相机拍摄的摆拍照。
- 非自拍也必须是**朋友或旁人用手机拍摄**的生活快照：手机镜头高度、轻微广角、自然构图和
  偶然抓拍感；不要自动升级成单反/无反相机、影棚人像或商业写真。
- 提示词中必须明确出现 `phone camera` / `smartphone photo` / `casual smartphone snapshot`
  这类手机拍摄信息，并根据方向写 `3:4 vertical` 或 `4:3 horizontal`。
- 手机镜头的特征要在：稍广的视角、近距离时轻微的边缘变形、手机 HDR 那种压过的高光和提亮
  的暗部、不过度干净的细节。光线用现场光——窗光、屋里的灯、屏幕的反光、路灯。
- 禁止单反/无反相机、专业摄影机、影棚三点布光、商业人像、杂志封面、广告大片、专业模特摆拍、
  过度后期和磨皮。浅景深可以有，但不能让画面像专业肖像摄影。

## 画面比例

- **4:3**，手机相机的原生比例。
- 竖着拍（自拍、半身、全身）就是 **3:4 竖版**；横着拍（风景、桌面、和别人合拍）
  就是 **4:3 横版**。按这次拍什么选方向，比例本身不变。

## 明确排除

- ❌ 插画 / 动漫 / 二次元 / 厚涂 / 线稿 / 水彩 / 油画 / CG / 3D 渲染
- ❌ **动漫脸**：过大的眼睛、尖到不真实的下巴、没有真实鼻梁——这是真人 cos，不是把动漫脸贴到照片上
- ❌ 单反 / 无反 / 专业摄影机 / 影棚 / 商业写真 / 杂志封面 / 广告大片 / 专业人像布光
- ❌ 画面里出现文字、水印、logo、签名、边框、多格拼图
- ❌ `masterpiece, best quality, 8k, ultra detailed` 这类标签式画质咒语
- ❌ 除要求里明确写了以外的其他人

---

## 可直接使用的英文风格短语

生图提示词用英文写，下面两组按当前配置的写法（`llm.draw_prompt_style`）取一组用，
不要两组混着堆。两组都必须保留手机拍摄方向。

**自然语言式（natural）：**

> A casual smartphone photo taken on an ordinary phone camera, 3:4 vertical for a selfie
> or 4:3 horizontal for a landscape or group composition. The subject has a believable
> Japanese-coser-like real-person cosplay aesthetic: a well-made wig, natural circle lenses,
> and polished but realistic cosplay makeup. The image feels like a candid everyday snapshot
> with available ambient light, natural skin texture and slight handheld imperfection — not a
> studio shoot, not editorial photography, not a professional portrait, not an illustration,
> and not an anime face.

**标签式（tags）：**

> `photorealistic, real photo, phone camera photo, casual smartphone snapshot, 3:4 vertical
> or 4:3 horizontal, Japanese-coser-like aesthetic, real-person cosplay, well-made wig,
> circle lenses, realistic cosplay makeup, natural skin texture, visible pores, ambient lighting,
> candid everyday feel, slight handheld imperfection, no studio, no editorial photography,
> no professional portrait, no DSLR, no mirrorless camera, (anime face:0), no text, no watermark`

---

## 维护约定

- 这是**主人的**风格偏好。主人说要改风格（"以后画成动漫风""不要浅景深"）时才改，
  不要自己动。
- 改完之后已有的参考图会和新风格不一致——风格换了要重新生成参考图，
  而覆盖参考图会改变你的形象，先征得主人同意。
- 参考图只吃这里的**渲染风格**（写实/动漫、质感、画质），不吃**拍摄方式和比例**：
  参考图必须是中性的形象锚（平背景、均匀光照、正对镜头），不该是一张随手拍的生活照。
