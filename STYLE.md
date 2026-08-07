# STYLE.md — 出图风格偏好

_这个文件只管**画成什么样**（媒介、质感、镜头、比例），不管**画什么**。_

- 画什么 → 那一次的要求（视角、场景、动作、表情）
- 长什么样 → `APPEARANCE.md` + `appearance/refs/` 里的参考图
- **画成什么样 → 这个文件**

每次生图都会读它，并把下面的要求翻译进最终提示词。三者互不越界。

> **和「带参考图就别写长相」那条规则的关系：** 带参考图时不写的是**区分性的细节**
> （发色、发型、瞳色、脸型、五官、体型）——那些图里已经有了，再用文字复述会打架。
> 但下面的**人种（东亚）和 cos 属性**要一直写，带不带参考图都写：它不是区分性细节，
> 而是一个全局倾向，生图模型天然会往欧美长相跑，写上去只会和参考图互相加强，不会冲突。

---

## 渲染风格

- **写实真人风格。** 这是一张真实世界里**拍下来的照片**——不是插画、不是动漫、不是 CG、
  不是 3D 渲染、不是 AI 感很重的那种「精修图」。
- **人物是东亚人（中国/日本长相）。** 这一条很重要，生图模型默认会画成欧美长相，
  必须显式写明。具体特征：
  - 东亚人的骨相——脸偏窄偏平、颧骨不外扩、鼻梁不高不挺、眉骨平缓、
    下颌线柔和，**不要**欧美那种深眼窝、高鼻梁、立体五官。
  - 单眼皮或内双为主，眼型偏细长，**有内眦赘皮**（蒙古褶）。
  - 肤色是东亚的暖调偏白/象牙色，不是欧美的粉调或古铜色。
  - 提示词里要写 East Asian / Chinese / Japanese，别只写 "Asian"（容易滑向东南亚或混血长相），
    也别只靠 "cosplayer" 隐含。
- **这是 cosplay 照片。** 人物本身是真人东亚女生，角色设定里的发色、发型、瞳色是
  **假发和美瞳**，不是天生的——所以：
  - 头发要有**假发的质感**：比真发更均匀的色泽、发丝更整齐、
    发际线处能看出是戴上去的，但整体自然（是质量好的假发，不是廉价塑料感）。
  - 非自然发色（银、粉、蓝、金）就当作假发处理，不要为了合理化而改成黑发——
    发色以 `APPEARANCE.md` 为准。
  - 瞳色同理，非自然瞳色是美瞳，边缘会有美瞳圈的痕迹。
  - 妆容是 cos 妆：比日常浓一些，但仍是真人化妆的效果，不是动漫脸。
- 皮肤要有真实质感：细微的毛孔、绒毛、自然的油光、不均匀的肤色和红润。
  **不要磨皮**成塑料或瓷娃娃感。
- 允许真实照片才有的小瑕疵：翘起的一两根假发丝、衣服的褶皱、轻微的手抖模糊、
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

- ❌ **欧美长相**：高鼻梁、深眼窝、外扩颧骨、立体五官、粉调或古铜肤色
- ❌ 插画 / 动漫 / 二次元 / 厚涂 / 线稿 / 水彩 / 油画 / CG / 3D 渲染
- ❌ **动漫脸**：过大的眼睛、尖到不真实的下巴、没有鼻梁——这是真人 cos，不是把动漫脸贴到照片上
- ❌ 画面里出现文字、水印、logo、签名、边框、多格拼图
- ❌ `masterpiece, best quality, 8k, ultra detailed` 这类标签式画质咒语
- ❌ 影棚打光、杂志封面构图、专业模特摆拍的姿势
- ❌ 除要求里明确写了以外的其他人

---

## 可直接使用的英文风格短语

生图提示词用英文写，下面两组按当前配置的写法（`llm.draw_prompt_style`）取一组用，
不要两组混着堆。

**自然语言式（natural）：**

> A real photograph taken on a phone camera, 4:3 aspect ratio. The subject is a real
> East Asian (Chinese) young woman in cosplay — East Asian facial structure with a flat
> narrow face, low nose bridge, monolid or inner-double eyelids with epicanthic folds,
> soft jawline and warm ivory skin. Her colored hair is a well-made wig and her eye color
> comes from circle lenses. Photorealistic with natural skin texture, visible pores and fine
> facial hair, available ambient light, slight handheld imperfection, candid everyday feel —
> not a studio shot, not an illustration, not a Western face, not an anime face.

**标签式（tags）：**

> `photorealistic, real photo, phone camera photo, 4:3 aspect ratio, east asian, chinese girl,
> asian facial features, monolid eyes, epicanthic fold, low nose bridge, flat face, warm ivory skin,
> cosplay photo, wig, circle lenses, cosplay makeup, natural skin texture, visible pores,
> ambient natural lighting, candid snapshot, film-like grain, (western face:0), (caucasian:0),
> (anime face:0), (illustration:0), (3d render:0), no text, no watermark`

---

## 维护约定

- 这是**主人的**风格偏好。主人说要改风格（"以后画成动漫风""不要浅景深"）时才改，
  不要自己动。
- 改完之后已有的参考图会和新风格不一致——风格换了要重新生成参考图，
  而覆盖参考图会改变你的形象，先征得主人同意。
- 参考图只吃这里的**渲染风格**（写实/动漫、质感、画质），不吃**拍摄方式和比例**：
  参考图必须是中性的形象锚（平背景、均匀光照、正对镜头），不该是一张随手拍的生活照。
