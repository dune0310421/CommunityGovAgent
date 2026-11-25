"""规范性检查：模板解析、格式检查、模板推荐及本项检查的评论内容生成。"""

from copy import deepcopy
import html
import json
import math
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from markdown_it import MarkdownIt
import yaml

from errors import IssueProcessingError

LLM_TIMEOUT = 120
RECOMMEND_PROMPT = Path(__file__).resolve().with_name("recommend_template.md")


class TemplateLoader(yaml.SafeLoader):
    """保留 Yes/No/On/Off 等选项文字，仅将 true/false 当作布尔值。"""


TemplateLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in rules if tag != 'tag:yaml.org,2002:bool']
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
TemplateLoader.add_implicit_resolver(
    'tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$', re.IGNORECASE), list('tTfF'))


def _label(text: str) -> str:
    """提取 Markdown 的显示文字，用于匹配标题和复选项。

    Args:
        text: 含强调、链接或行内代码的文字。
    Returns:
        去除格式标记及首尾空白的显示文字。
    """
    tokens = MarkdownIt("commonmark").parseInline(text)
    return "".join(token.content for item in tokens for token in (item.children or [])
                   if token.type in {"text", "code_inline", "image"}).strip()


def _section_key(text: str) -> str:
    """生成章节匹配键，忽略空白、大小写、末尾冒号及必填／选填提示。

    仅处理独立提示（含中英双语），保留字段名称中的其他括号内容；
    不用于判断必填规则或匹配复选框选项。
    """
    label = "".join(_label(text).split()).casefold()
    label = label.translate(str.maketrans({"（": "(", "）": ")", "／": "/"}))
    hint = r"(?:必填|必选|选填|可选|required|mandatory|optional)"
    hints = hint + r"(?:[/／|、]" + hint + r")*"
    suffix = rf"(?:\({hints}\)|（{hints}）|\[{hints}\])"
    # 冒号可以位于提示前后；每轮仅移除末尾格式，不删除业务括号。
    while True:
        stripped = re.sub(rf"(?:{suffix})+$", "", label.rstrip(":："))
        if not stripped or stripped == label:
            return label
        label = stripped


def _section_forms(key: str) -> set[str]:
    """提取双语书写形式和斜杠分隔的可选名称，不推断同义词。"""
    def language(part: str) -> str | None:
        chinese = bool(re.search(r"[\u3400-\u9fff]", part))
        english = bool(re.search(r"[a-z]", part))
        # 带限定括号的名称不能靠拆分丢掉限定条件。
        if re.search(r"[()\[\]]", part):
            return None
        if chinese and not english:
            return "zh"
        if english and not chinese:
            return "en"
        return None

    pair = None
    parenthetical = re.fullmatch(r"([^()]+)\(([^()]+)\)", key)
    if parenthetical:
        left, right = parenthetical.groups()
        if {language(left), language(right)} == {"zh", "en"}:
            pair = (left, right)
    else:
        parts = key.split("/")
        languages = [language(part) for part in parts]
        # 先识别两种语言的完整名称，再提取各侧的斜杠选项。
        switches = [i for i in range(1, len(parts)) if languages[i] != languages[i - 1]]
        if None not in languages and set(languages) == {"zh", "en"} and len(switches) == 1:
            cut = switches[0]
            pair = ("/".join(parts[:cut]), "/".join(parts[cut:]))
    forms = {key}
    if pair:
        left, right = pair
        forms.update((left, right, f"{left}/{right}", f"{right}/{left}",
                      f"{left}({right})", f"{right}({left})"))
    # 只拆独立的斜杠选项；括号中的业务限定（如版本(开发/生产)）保持完整。
    forms.update(part for form in tuple(forms) if not re.search(r"[()\[\]]", form)
                 for part in form.split("/") if part)
    return forms


def _resolve_section_name(key: str, field_names: dict[str, str]) -> str | None:
    """任一语言或斜杠选项匹配即可；指向不同字段时不自动匹配。"""
    if key in field_names:
        return field_names[key]
    targets = {field_names[form] for form in _section_forms(key) if form in field_names}
    return next(iter(targets)) if len(targets) == 1 else None


def _sections(
    body: str, *, normalize_hints: bool = False, field_keys: set[str] | None = None,
    field_names: dict[str, str] | None = None,
    field_types: dict[str, str] | None = None,
) -> dict[str, str]:
    """提取 Markdown 章节及内容，忽略代码块和引用中的伪标题。

    Args:
        body: Issue 的原始 Markdown 正文。
        normalize_hints: 匹配 Issue 时忽略章节名末尾的必填／选填提示；模板解析保留原名。
        field_keys: 检查 Issue 时的模板字段键；提供时以这些字段识别章节。
        field_names: 章节书写形式到模板字段键的映射，仅用于 Issue 检查。
        field_types: 模板字段键到类型的映射；input/dropdown 在下一个正文标题处结束，
            长文本仍延续到下一个模板字段，允许内部使用任意层级标题。
    Returns:
        忽略空白后的章节标题到填写内容的映射；同名章节合并，子节内容计入父节。
    """
    # 将正文解析为「章节标题 → 填写内容」，代码块和引用中的标题不算章节。
    tokens = MarkdownIt("commonmark").parse(body)
    headings = [
        (i, int(token.tag[1:]), _section_key(tokens[i + 1].content) if normalize_hints
         else "".join(_label(tokens[i + 1].content).split()))
        for i, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0
    ]
    all_headings = headings
    if field_names is not None:
        headings = [(start, level, resolved) for start, level, label in headings
                    if (resolved := _resolve_section_name(label, field_names)) is not None]
    if field_keys is not None:
        headings = [heading for heading in headings if heading[2] in field_keys]
    sections: dict[str, list[str]] = {}
    for position, (start, level, label) in enumerate(headings):
        if field_types and field_types.get(label) in {"input", "dropdown"}:
            # 短字段到下一个正文标题结束，包括模板未定义的章节。
            end = next((i for i, _, _ in all_headings if i > start), len(tokens))
        else:
            # 模板本身按层级解析；Issue 长文本中的自加标题保留在当前字段内。
            end = next(
                (i for i, depth, _ in headings[position + 1:]
                 if field_keys is not None or depth <= level),
                len(tokens),
            )
        parts = []
        for i in range(start + 3, end):
            token = tokens[i]
            if token.type in {"fence", "code_block"}:
                parts.append(token.content)
            elif token.type in {"inline", "html_block"}:
                # 子标题本身不算填写内容；HTML 注释也不算。
                if tokens[i - 1].type != "heading_open":
                    parts.append(re.sub(r"<!--[\s\S]*?-->", "", token.content))
        sections.setdefault(label, []).append("\n".join(parts).strip())
    return {label: "\n".join(parts).strip() for label, parts in sections.items()}


def _required_hint(*texts: str | None) -> bool:
    """识别字段说明中的明确必填指示。

    Args:
        texts: 字段名称、描述、占位提示或章节说明；允许 None。
    Returns:
        存在必填、必选、必须填写／选择／勾选、required、mandatory、
        must fill/select/check，或名称首尾的单个星号时返回 True；忽略代码块。
    """
    for text in texts:
        if not text:
            continue
        tokens = MarkdownIt("commonmark").parse(text.replace("<!--", "").replace("-->", ""))
        plain = " ".join(_label(token.content) for token in tokens
                         if token.type in {"inline", "html_block"})
        plain = re.sub(r"(?:非|不(?:是|为)?|无需|无须)\s*(?:必填|必选)|"
                       r"\b(?:not\s+(?:a\s+)?required|not\s+mandatory|non[- ](?:required|mandatory)|optional)\b", "", plain,
                       flags=re.IGNORECASE)
        if re.search(r"必填|必选|必须\s*(?:填写|填入|提供|选择|勾选)|"
                     r"\b(?:required|mandatory|must\s+(?:fill|provide|select|check))\b|"
                     r"^[*＊][^*＊\s]|[^*＊][*＊]\s*$", plain, re.IGNORECASE):
            return True
    return False


def _markdown_fields(body: str, *, required: bool | None = None) -> list[dict[str, Any]]:
    """把 Markdown 填写章节转成文本字段，默认必填。

    Args:
        body: Markdown 模板正文，或文本框中预置的 Markdown。
        required: 外层文本框的必填配置；False 使无明确标记的章节选填。
    Returns:
        textarea 字段列表，章节原文作为占位内容；只检查末级章节，
        必填／选填标记向子节继承，必填指示优先；说明章节及其子节跳过。
        代码块、引用和列表内的标题不作为字段。
    """
    tokens = MarkdownIt("commonmark").parse(body)
    headings = [(i, int(token.tag[1:]), _label(tokens[i + 1].content))
                for i, token in enumerate(tokens)
                if token.type == "heading_open" and token.level == 0]
    sections = _sections(body)
    instructions = {"填写说明", "提交说明", "使用说明", "instructions", "submissioninstructions"}
    optional = re.compile(r"(?:[（(\[]\s*(?:选填|可选|optional)\s*[）)\]]|(?:^|[：:])\s*(?:选填|可选|optional))\s*$", re.IGNORECASE)
    parents: list[tuple[int, bool, bool, bool]] = []
    fields = []
    for index, (start, level, label) in enumerate(headings):
        parents = [parent for parent in parents if parent[0] < level]
        label_key = "".join(label.split())
        is_optional = bool(optional.search(label_key)) or any(parent[1] for parent in parents)
        is_instruction = optional.sub("", label_key).casefold() in instructions or any(parent[2] for parent in parents)
        explicitly_required = _required_hint(label) or any(parent[3] for parent in parents)
        parents.append((level, is_optional, is_instruction, explicitly_required))
        # 有子标题的章节作为分组，避免重复检查或要求选填分组必须填写。
        if is_instruction or (index + 1 < len(headings) and headings[index + 1][1] > level):
            continue
        section = sections.get(label_key, "")
        end = headings[index + 1][0] if index + 1 < len(headings) else len(tokens)
        # HTML 注释中的填写要求也有效，但代码示例中的词语不作为必填指示。
        guidance = " ".join(token.content for token in tokens[start + 3:end]
                            if token.type in {"inline", "html_block"})
        fields.append({"type": "textarea", "attributes": {
            "label": label, "placeholder": section},
            "validations": {"required": explicitly_required or _required_hint(guidance)
                            or (required is not False and not is_optional)}})
    return fields


def _option_label(value: Any) -> str:
    # 官方示例包含未加引号的版本号；展示时将数值转回文字。
    """将下拉选项转换为显示文字。

    Args:
        value: 选项文字或数值版本号。
    Returns:
        原始文字或数值转成的字符串。
    Raises:
        ValueError: 选项为空、类型无效或数值不是有限值。
    """
    if isinstance(value, str) and value.strip():
        return value
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return str(value)
    raise ValueError('选项需要非空文字或版本号')


def _fields(template: dict[str, Any], *, expand_markdown: bool = True) -> list[dict[str, Any]]:
    """校验并提取输入字段，markdown 仅用于识别紧邻复选框的全部勾选要求。

    Args:
        template: 已解析的模板字典。
        expand_markdown: 是否展开文本框中的 Markdown 章节；复制模板时关闭以保留原文。
    Returns:
        规范化后的字段副本列表；下拉选项统一为字符串，含 Markdown 章节的文本框按章节展开，不依赖 label。关闭展开时用 markdown_body 保留原文。原模板不变。
    Raises:
        ValueError: body、字段类型或属性格式无效。
    """
    index = None
    try:
        body = template.get('body', [])
        if not isinstance(body, list):
            raise ValueError('body 必须为数组')
        result = []
        for index, original in enumerate(body):
            if not isinstance(original, dict):
                raise ValueError('字段必须为对象')
            field = deepcopy(original)
            kind = field.get('type')
            if kind not in {'markdown', 'input', 'textarea', 'dropdown', 'checkboxes'}:
                raise ValueError('不支持的表单元素类型')
            attrs = field.get('attributes')
            if not isinstance(attrs, dict):
                raise ValueError('attributes 必须为对象')
            if kind == 'markdown':
                if not isinstance(attrs.get('value'), str):
                    raise ValueError('markdown.value 必须为文字')
                continue
            for key in ('description', 'placeholder', 'value', 'render'):
                if attrs.get(key) is not None and not isinstance(attrs[key], str):
                    raise ValueError('字段文字属性无效')
            validations = field.setdefault('validations', {})
            if not isinstance(validations, dict) or not isinstance(validations.get('required', False), bool):
                raise ValueError('validations.required 必须为布尔值')
            label = attrs.get('label')
            if label is not None and not isinstance(label, str):
                raise ValueError('字段 label 必须为文字')
            # 配置和文字取并集：显式 false 不能抵消字段本身的必填说明。
            if _required_hint(label, attrs.get('description'), attrs.get('placeholder')):
                validations['required'] = True
            # 外层必填表示文本框需要内容，具体章节仍各自识别必填／选填。
            sections = (_markdown_fields(attrs.get('value') or '',
                        required=validations.get('required'))
                        if kind == 'textarea' and not attrs.get('render') else [])
            if sections:
                if expand_markdown:
                    result.extend(sections)
                else:
                    field['markdown_body'] = attrs['value']
                    result.append(field)
                continue
            if not label or not label.strip():
                raise ValueError('字段缺少可检查的 label 或 Markdown 填写章节')
            if kind in {'input', 'textarea'} and not attrs.get('render') and _required_hint(attrs.get('value')):
                validations['required'] = True
            if kind in {'dropdown', 'checkboxes'}:
                options = attrs.get('options')
                if not isinstance(options, list) or not options:
                    raise ValueError('options 必须为非空数组')
                if kind == 'dropdown':
                    # 文档既给出字符串列表，也给出 label 对象形式。
                    attrs['options'] = [_option_label(o.get('label') if isinstance(o, dict) else o)
                                        for o in options]
                    if not isinstance(attrs.get('multiple', False), bool):
                        raise ValueError('multiple 必须为布尔值')
                    if 'default' in attrs and (type(attrs['default']) is not int
                            or not 0 <= attrs['default'] < len(options)):
                        raise ValueError('default 必须为有效选项索引')
                else:
                    # 仅将紧邻的说明应用于当前组，不影响后续其他复选框。
                    previous = body[index - 1] if index else {}
                    notice = previous['attributes']['value'] if previous.get('type') == 'markdown' else ''
                    all_required = any(re.search(
                        r'(?:必须|务必|请)(?:全部|所有)(?:确认(?:且|并))?勾选',
                        ''.join(_label(token.content).split()))
                        for token in MarkdownIt("commonmark").parse(notice) if token.type == 'inline')
                    for option in options:
                        if (not isinstance(option, dict) or not isinstance(option.get('label'), str)
                                or not option['label'].strip()
                                or not isinstance(option.get('required', False), bool)):
                            raise ValueError('复选项需要 label 和可选的布尔 required')
                        option['required'] = all_required or option.get('required', False) or _required_hint(option['label'])
            result.append(field)
        return result
    except ValueError as exc:
        location = f'body 第 {index + 1} 项：' if index is not None else ''
        raise ValueError(location + str(exc)) from None
    except (TypeError, KeyError, AttributeError):
        raise ValueError('模板字段或属性格式无效') from None


def parse_template(content: str, filename: str) -> dict[str, Any]:
    """解析 YAML 或 Markdown 模板并校验字段。

    Args:
        content: 模板文件的 UTF-8 文本。
        filename: 模板文件名，通过扩展名区分 YAML 和 Markdown。
    Returns:
        模板字典；Markdown 章节转为默认必填的文本字段，另保留 format 和
        markdown_body。明确选填、说明章节和分组标题不产生必填要求。
    Raises:
        ValueError: 模板信息、字段属性或 Markdown 头部配置无效。
        yaml.YAMLError: YAML 语法错误或包含不允许的标签。
    """
    content = content.lstrip('\ufeff')
    if PurePosixPath(filename).suffix.lower() == '.md':
        metadata = {}
        if content.startswith('---\n') or content.startswith('---\r\n'):
            lines = content.splitlines(keepends=True)
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in {'---', '...'}), None)
            if end is None:
                raise ValueError('Markdown front matter 未闭合')
            metadata = yaml.load(''.join(lines[1:end]), Loader=TemplateLoader)
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise ValueError('Markdown front matter 必须为对象')
            content = ''.join(lines[end + 1:])
        template = {**metadata, 'name': metadata.get('name') or PurePosixPath(filename).stem,
                    'description': metadata.get('about', metadata.get('description', '')),
                    'body': _markdown_fields(content), 'format': 'markdown', 'markdown_body': content}
    else:
        template = yaml.load(content, Loader=TemplateLoader)
    if (not isinstance(template, dict) or not isinstance(template.get('name'), str)
            or not template['name'].strip()):
        raise ValueError('模板 name 必须为非空文字')
    if not isinstance(template.get('title', ''), str):
        raise ValueError('模板 title 必须为文字')
    if not isinstance(template.get('description', ''), str):
        raise ValueError('模板 description 必须为文字')
    _fields(template)
    return template


def _checkbox_sections(
    body: str, *, field_keys: set[str] | None = None,
    field_names: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    """提取各章节中已勾选的列表选项。

    Args:
        body: Issue 的原始 Markdown 正文。
        field_keys: 检查 Issue 时的模板字段键；提供时勾选仅归入最近出现的模板字段。
        field_names: 与文本字段共用的章节书写形式映射。
    Returns:
        章节标题到已勾选选项集合的映射；子节勾选计入父节，
        忽略代码块、引用及普通文字中的勾选标记。
    """
    tokens = MarkdownIt("commonmark").parse(body)
    sections: dict[str, set[str]] = {}
    headings: list[tuple[int, str]] = []
    list_depth = quote_depth = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type == "list_item_open":
            list_depth += 1
        elif token.type == "list_item_close":
            list_depth -= 1
        elif token.type == "heading_open" and token.level == 0:
            depth = int(token.tag[1:])
            key = _section_key(tokens[index + 1].content)
            if field_names is not None:
                key = _resolve_section_name(key, field_names)
            if field_keys is not None:
                if key in field_keys:
                    headings = [(depth, key)]
                elif headings and depth <= headings[-1][0]:
                    # 同级或更高的非模板标题开始新内容，避免把其他章节的勾选项归入当前字段。
                    headings = []
            else:
                headings = [(d, label) for d, label in headings if d < depth]
                headings.append((depth, key))
        elif token.type == "inline" and list_depth and not quote_depth:
            match = re.match(r"^\[[xX]\]\s+(.+)$", token.content, re.DOTALL)
            if match:
                for _, label in headings:
                    sections.setdefault(label, set()).add("".join(_label(match.group(1)).split()))
    return sections


def _matches_dropdown_choice(value: str, options: list[str]) -> bool:
    """匹配下拉选项，允许唯一类别的末尾括号说明被补充、细化或省略。

    Args:
        value: Issue 中填写的单个选项。
        options: 模板定义的候选选项列表。
    Returns:
        忽略空白后完全匹配，或去除末尾括号说明后唯一对应某个类别时为 True。
        非完全匹配时，若多个候选仅靠括号区分、类别为空或不在候选中，则为 False。
    """
    value = "".join(value.split())
    choices = {"".join(option.split()) for option in options}
    if value in choices:
        return True
    # 仅处理末尾成对的括号说明；类别名称本身必须一致，不做子串或模糊匹配。
    annotation = r"(?:\([^()（）]*\)|（[^()（）]*）)$"
    category = re.sub(annotation, "", value)
    return bool(category) and sum(re.sub(annotation, "", option) == category for option in choices) == 1


def check_issue(
    title: str, body: str, templates: list[dict[str, Any]],
) -> dict[str, Any]:
    """检查标题前缀和必填章节，不调用网络或判断内容语义。

    Args:
        title: Issue 标题。
        body: Issue 的原始 Markdown 正文。
        templates: 待匹配的 YAML/Markdown 模板字典列表，不能为空。
    Returns:
        一个字典，包含三个字段：
            status 为 pass/no_match；
            matched_templates 为完全匹配的模板名称；
            checks 为各模板的匹配情况，包含 template 名称及 problems 列表，每个问题包含 field、reason 和 message；如果模板缺少可检查的规则还会附加 limitation。
    Raises:
        ValueError: 模板列表为空或模板字段格式无效。
    """
    if not templates:
        raise ValueError("至少需要一个模板")

    # 本地检查：逐一对照模板的标题前缀、必填章节及占位内容
    title_key = "".join(title.split())
    checks = [] # 记录各模板的检查结果
    for template in templates:
        fields = _fields(template)
        field_types = {_section_key(field["attributes"]["label"]): field["type"] for field in fields}
        field_keys = set(field_types)
        # 只保留无歧义的章节写法，完整字段名优先。
        candidates: dict[str, set[str]] = {}
        for key in field_keys:
            for form in _section_forms(key):
                candidates.setdefault(form, set()).add(key)
        field_names = {form: next(iter(targets)) for form, targets in candidates.items() if len(targets) == 1}
        field_names.update({key: key for key in field_keys})
        sections = _sections(body, normalize_hints=True, field_keys=field_keys,
                             field_names=field_names, field_types=field_types)
        selected_options = _checkbox_sections(body, field_keys=field_keys, field_names=field_names)
        markdown = template.get("format") == "markdown"
        problems = []
        prefix = template.get("title", "").strip()
        prefix_key = "".join(prefix.split())
        title_matches = title_key.startswith(prefix_key)
        if not title_matches and prefix_key.endswith((":", "：")) and len(prefix_key) > 1:
            stem = prefix_key[:-1]
            # 闭合括号已构成边界，允许 [Bug]启动失败；普通文字仍需分隔，避免匹配 Buggy。
            if stem.endswith(("]", "】", ")", "）")):
                title_matches = title_key.startswith(stem)
            else:
                pattern = r"\s*".join(re.escape(char) for char in stem)
                title_matches = re.match(r"\s*" + pattern + r"(?=\s|[:：]|$)", title) is not None
        # 检查标题前缀
        if not title_matches:
            problems.append({"field": "title", "reason": "title_prefix",
                             "message": f"标题应以 {prefix!r} 开头"})
        # 必填规则已统一为配置与文字指示的并集；已填写的选填下拉框也校验候选值。
        for field in fields:
            attributes = field["attributes"]
            label = attributes["label"].strip()
            section_key = _section_key(label)
            required = field.get("validations", {}).get("required", False)
            required_options = ([option for option in attributes["options"] if option.get("required", False)]
                                if field["type"] == "checkboxes" else [])
            if not (required or required_options) and (field["type"] != "dropdown" or section_key not in sections):
                continue
            if section_key not in sections:
                problems.append({"field": label, "reason": "missing_section",
                                 "message": "缺少此必填章节"})
                continue
            if field["type"] == "checkboxes":
                selected = selected_options.get(section_key, set())
                valid = {"".join(_label(option["label"]).split()) for option in attributes["options"]}
                if required and not selected.intersection(valid):
                    problems.append({"field": label, "reason": "unchecked_required",
                                     "message": "请至少勾选一个选项"})
                for option in required_options:
                    if "".join(_label(option["label"]).split()) not in selected:
                        problems.append({"field": label, "reason": "unchecked_required",
                                         "message": "请勾选必选项：" + option["label"]})
                continue
            content = sections[section_key]
            placeholders = {"", "_No response_", "No response",
                            (attributes.get("placeholder") or "").strip()}
            # 下拉选项或预填值本身恰为占位词时，仍按模板允许的真实值处理。
            allowed = attributes.get("options", []) if field["type"] == "dropdown" else []
            # YAML 和 Markdown 使用同一规则；仅改变占位提示的空白仍算未填写。
            content_key = "".join(content.split())
            is_placeholder = content_key in {"".join(item.split()) for item in placeholders}
            is_allowed = content_key in {"".join(item.split()) for item in allowed}
            is_prefilled = bool(content_key) and content_key == "".join((attributes.get("value") or "").split())
            if is_placeholder and not is_allowed and not is_prefilled:
                if required:
                    problems.append({"field": label, "reason": "empty_section",
                                     "message": "此必填章节为空或仅含占位内容"})
                continue
            if field["type"] == "dropdown" and not is_allowed:
                valid = _matches_dropdown_choice(content_key, allowed) or (
                    attributes.get("multiple", False) and all(
                        _matches_dropdown_choice(option, allowed) for option in content_key.split(",")))
                if not valid:
                    problems.append({"field": label, "reason": "invalid_option",
                                     "message": "请从模板候选项中选择" +
                                     ("一个或多个选项：" if attributes.get("multiple", False) else "一个选项：") +
                                     "、".join(allowed)})
        check = {"template": template["name"], "problems": problems}
        if markdown and not any(field.get("validations", {}).get("required", False) for field in fields):
            check["limitation"] = "Markdown 模板没有可检查的必填章节，无法判定格式是否通过。"
        elif not prefix_key and not any(
            field.get("validations", {}).get("required", False)
            or (field["type"] == "checkboxes" and any(
                option.get("required", False) for option in field["attributes"]["options"]))
            for field in fields
        ):
            check["limitation"] = "模板缺少可自动检查的标题或必填规则，无法判定格式是否通过。"
        checks.append(check)

    # 判断是否完全匹配
    complete = [check["template"] for check in checks if not check["problems"] and not check.get("limitation")]
    return {"status": "pass" if complete else "no_match", "matched_templates": complete, "checks": checks}


def recommend_template(
    title: str, body: str, templates: list[dict[str, Any]],
    agent_api_key: str, model: str = "", base_url: str = "",
) -> dict[str, Any]:
    """启动一次 Claude Code 任务，选择适用模板并逐项复核非完全匹配的 Issue。

    Args:
        title: Issue 原始标题。
        body: Issue 原始 Markdown 正文。
        templates: 仓库候选模板字典列表。
        agent_api_key: Claude Code 兼容接口的 Bearer 认证密钥；为空时不调用模型。
        model: 服务商提供的模型名称。
        base_url: 服务商提供的 Anthropic 兼容接口地址。
    Returns:
        一个字典，包含以下字段：
            status 为 pass / needs_changes / recommended / not_applicable / insufficient_info；
            template 为匹配或推荐的模板名称；
            reason 为原因；
            maintainer 为可选的维护者建议文本；
            problems 为模型确认的具体问题列表，仅在 status 为 needs_changes 时返回，每项包含 field、reason 和 message。
    Raises:
        IssueProcessingError: 配置缺失、调用失败、超时或响应无效；交由任务队列重试。
    """
    if not agent_api_key.strip():
        raise IssueProcessingError("未配置 Agent Key，无法执行模板推荐")
    if not model.strip() or not base_url.strip():
        raise IssueProcessingError("未配置 LLM_BASE_URL 或 LLM_MODEL，无法执行模板推荐")
    # 将模板信息整理为json传给模型
    candidates = [{
        "name": item["name"], "description": item.get("description", ""),
        "title": item.get("title", ""),
        "markdown_body": item.get("markdown_body", ""),
        "fields": [{"label": field["attributes"]["label"],
                    "description": field["attributes"].get("description", ""),
                    "type": field["type"], "options": field["attributes"].get("options", []),
                    "multiple": field["attributes"].get("multiple", False),
                    "placeholder": field["attributes"].get("placeholder", ""),
                    "value": field["attributes"].get("value", ""),
                    "required": field.get("validations", {}).get("required", False)}
                   for field in _fields(item)],
    } for item in templates]

    # 启动子进程运行 Claude Code
    try:
        # 准备 Claude Code 的环境变量
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
               if key in os.environ}
        env.update({
            "ANTHROPIC_AUTH_TOKEN": agent_api_key,
            "ANTHROPIC_BASE_URL": base_url.strip(),
            "API_TIMEOUT_MS": str(LLM_TIMEOUT * 1000),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        })

        # 准备 Claude Code 命令和数据
        command = [
            "claude", "--bare", "-p", "--model", model.strip(),
            "--tools", "", "--no-session-persistence", "--max-turns", "3",
            "--output-format", "json",
            "--system-prompt", RECOMMEND_PROMPT.read_text(encoding="utf-8"),
        ]
        data = json.dumps({"issue": {"title": title, "body": body},
                           "templates": candidates}, ensure_ascii=False)

        # 新建子进程和临时目录，运行 Claude Code
        with tempfile.TemporaryDirectory(prefix="issue-gov-") as directory:
            env["CLAUDE_CONFIG_DIR"] = str(Path(directory) / ".claude")
            with subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=directory, env=env, start_new_session=True,
            ) as process:
                try:
                    output, stderr = process.communicate(data, timeout=LLM_TIMEOUT)
                finally:
                    # 清理子进程
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

        # 解析输出
        try:
            envelope = json.loads(output)
        except ValueError:
            raise ValueError(stderr.strip() or output.strip() or "Claude Code 未返回结果") from None
        if not isinstance(envelope, dict):
            raise ValueError("Claude Code 返回格式无效")
        if (process.returncode != 0 or envelope.get("type") != "result"
                or envelope.get("subtype") != "success" or envelope.get("is_error") is not False):
            raise ValueError(envelope.get("result") or envelope.get("errors")
                             or stderr.strip() or "Claude Code 调用失败")
        # 解析 Agent 推荐结果
        suggestion = json.loads(envelope["result"])
        if not isinstance(suggestion, dict):
            raise ValueError("推荐结果格式无效")
        status = suggestion["status"]
        name = suggestion["template"]
        reason = suggestion["reason"]
        maintenance = suggestion.get("maintainer")
        # 校验推荐状态
        if status not in {"pass", "needs_changes", "recommended", "not_applicable", "insufficient_info"}:
            raise ValueError("推荐状态无效")
        # 校验模板名
        if status in {"pass", "needs_changes", "recommended"}:
            if name not in {t["name"] for t in templates}:
                raise ValueError("推荐模板无效")
        elif name is not None:
            raise ValueError("未推荐时不能指定模板")
        # 校验推荐理由
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("推荐理由无效")
        # 校验维护者建议
        if status == "not_applicable" and maintenance is None:
            raise ValueError("没有适用模板时须提供维护者建议")
        if maintenance is not None:
            if not isinstance(maintenance, str) or not 1 <= len(maintenance.strip()) <= 500:
                raise ValueError("维护者建议无效")
            maintenance = maintenance.strip()
        result = {"status": status, "template": name, "reason": reason.strip(),
                  "maintainer": maintenance}
        if status == "needs_changes":
            # 校验问题列表
            problems = suggestion["problems"]
            if not isinstance(problems, list) or not problems:
                raise ValueError("需要修改时须提供具体问题")
            selected = next(t for t in candidates if t["name"] == name)
            labels = {field["label"] for field in selected["fields"]}
            # 逐条检查问题格式
            for problem in problems:
                if not isinstance(problem, dict):
                    raise ValueError("问题格式无效")
                if problem["reason"] not in {"title_prefix", "body"}:
                    raise ValueError("问题类型无效")
                allowed_fields = {"title"} if problem["reason"] == "title_prefix" else labels
                if problem["field"] not in allowed_fields:
                    raise ValueError("问题字段不属于所选模板")
                if not isinstance(problem["message"], str) or not 1 <= len(problem["message"].strip()) <= 200:
                    raise ValueError("问题说明无效")
            result["problems"] = problems
        return result
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "模型响应超时，未能完成模板推荐。"
        elif isinstance(exc, OSError):
            reason = "推荐程序运行失败，未能完成模板推荐。"
        else:
            reason = "模型未返回有效的推荐结果，未能完成模板推荐。"
        raise IssueProcessingError(reason) from None


def _escape_comment_text(value: str) -> str:
    """将模型文本转为评论中的单行文字，避免产生 HTML、链接或提及。

    Args:
        value: 已验证的模板名或理由。
    Returns:
        转义后的单行评论文字。
    """
    value = html.escape(" ".join(value.split()), quote=False).replace("@", "@\u200b")
    return re.sub(r"([\\`*_{\[\]}()!#>|~])", r"\\\1", value)


def render_comment_template(key: str, **fields: Any) -> str:
    """填入独立评论模板的占位符，不解析字段内容中的花括号。

    Args:
        key: comment_templates.yml 中的模板条目名称。
        **fields: 占位符名称及对应内容；须提供选中条目使用的全部占位符。
    Returns:
        完成一次占位符替换的 Markdown 字符串，字段内容不会被再次解析。
    Raises:
        OSError: 评论模板文件无法读取。
        yaml.YAMLError: 评论模板文件的 YAML 语法无效。
        KeyError: 模板条目不存在，或缺少必需的占位符值。
        ValueError: 模板中的格式占位符语法无效。
    """
    path = Path(__file__).resolve().with_name("comment_templates.yml")
    templates = yaml.safe_load(path.read_text(encoding="utf-8"))
    return templates[key].format_map(fields)


def _format_template(template: dict, *, show_title: bool = True, show_body: bool = True,
                     show_name: bool = False) -> list[str]:
    """将仓库模板转为含可复制标题和 Markdown 正文的折叠块。

    Args:
        template: 已解析的 YAML 或 Markdown 模板。
        show_title: 是否展示可复制标题。
        show_body: 是否展示可复制正文。
        show_name: 是否在折叠栏显示名称，用于区分多个候选模板。
    Returns:
        折叠块的 Markdown 文本行列表，仅包含开关指定的标题、正文和名称。
        YAML 仅复制填写字段，保留字段提示、预填值和选项，跳过 markdown 展示说明；
        不填入 Issue 内容，Markdown 文件正文及文本框中的预置 Markdown 原样保留。
    Raises:
        ValueError: YAML 模板字段定义无效。
    """
    title = template.get("title", "")
    if template.get("format") == "markdown":
        body = template["markdown_body"]
    else:
        parts = []
        for field in _fields(template, expand_markdown=False):
            attrs = field["attributes"]
            if 'markdown_body' in field:
                if attrs.get('description'):
                    parts.extend(['<!-- ' + attrs['description'] + ' -->', ''])
                parts.extend([field['markdown_body'], ''])
                continue
            parts.extend(["### " + attrs["label"], ""])
            required = field.get("validations", {}).get("required") or (
                field["type"] == "checkboxes" and any(option.get("required") for option in attrs["options"]))
            hints = ["必填" if required else "选填"]
            hints.extend(attrs[key] for key in ("description", "placeholder") if attrs.get(key))
            if field["type"] == "dropdown":
                hints.append(("可多选：" if attrs.get("multiple") else "请选择一项：") + "、".join(attrs["options"]))
            parts.extend(["<!-- " + "\n".join(hints) + " -->", ""])
            if field["type"] == "checkboxes":
                content = "\n".join("- [ ] " + option["label"] + (" <!-- 必选 -->" if option.get("required") else "")
                                    for option in attrs["options"])
            elif field["type"] == "dropdown":
                content = attrs["options"][attrs["default"]] if "default" in attrs else ""
            else:
                content = attrs.get("value") or ""
                if attrs.get("render"):
                    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", content)), default=0))
                    content = f"{fence}{attrs['render']}\n{content}\n{fence}"
            parts.extend([content, ""])
        body = "\n".join(parts).strip()

    def code_fence(text: str) -> str:
        """生成不会被内容中的反引号提前闭合的代码围栏。

        Args:
            text: 要放入代码块的原始文本。
        Returns:
            至少三个反引号组成的字符串，长度大于文本中最长的连续反引号。
        """
        return "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))

    content = []
    if show_title:
        content.append(render_comment_template("template_title", title=title, title_fence=code_fence(title)))
    if show_body:
        content.append(render_comment_template("template_body", body=body, body_fence=code_fence(body)))
    name = html.escape(" ".join(template["name"].split()), quote=False).replace("@", "@\u200b") + "：" if show_name else ""
    return render_comment_template("template", name=name, content="\n\n".join(content)).split("\n")


def format_compliance_check(result: dict[str, Any], *,
                             templates: list[dict] | tuple[dict, ...] = ()) -> str:
    """根据检查结果生成通过通知或需要贡献者修改 Issue 的评论。

    Args:
        result: 检查结果。通过时 status 为 pass，或未匹配且 recommendation.status 为
            not_applicable；未匹配时包含 recommendation，其 status 为 needs_changes、
            recommended 或 insufficient_info，needs_changes 的 problems 为模型确认的缺项。
        templates: 已解析的仓库模板，提供可复制的标题和正文。
    Returns:
        完整 Markdown 检查章节。通过时返回通过通知；需要完善时仅展示有问题的标题或正文；
        推荐成功展示指定模板，信息不足展示所有候选模板。
    Raises:
        IssueProcessingError: 检查或推荐状态不属于需要贡献者修改的有效结果。
    """
    # 1. 通过
    if result["status"] == "pass" or (
            result["status"] == "no_match"
            and result.get("recommendation", {}).get("status") == "not_applicable"):
        return render_comment_template("passed")
    # 2. 未完全匹配时，依据模型复核结果生成完善或推荐建议。
    if result["status"] == "no_match":
        recommendation = result.get("recommendation", {})
        decision = recommendation.get("status")
        # 2.1 部分匹配
        if decision == "needs_changes":
            name = recommendation["template"]
            problems = recommendation["problems"]
            problem_text = "\n".join(
                f"- {_escape_comment_text(p['field'])}：{_escape_comment_text(p['message'])}"
                for p in problems)
            selected = next((t for t in templates if t["name"] == name), None)
            template_text = ""
            if selected is not None:
                template_text = "\n".join(_format_template(
                    selected, show_title=any(p["reason"] == "title_prefix" for p in problems),
                    show_body=any(p["reason"] != "title_prefix" for p in problems)))
            return render_comment_template("needs_changes", name=_escape_comment_text(name),
                                           problems=problem_text, template=template_text)
        # 2.2 未匹配
        if decision == "recommended":
            # 已推荐具体模板：展示推荐原因和该模板的完整标题、正文
            name = recommendation["template"]
            selected = next((t for t in templates if t["name"] == name), None)
            template_text = ""
            if selected is not None:
                template_lines = _format_template(selected)
                template_text = "\n".join(template_lines)
            # 渲染评论
            return render_comment_template("recommended", reason=_escape_comment_text(recommendation["reason"]), name=_escape_comment_text(name), template=template_text)
        # 2.3信息不足：展示所有仓库模板，并在折叠栏注明名称，供贡献者补充后选择。
        if decision == "insufficient_info":
            template_blocks = ["\n".join(_format_template(t, show_name=True)) for t in templates]
            # 渲染评论
            return render_comment_template("insufficient_info", reason=_escape_comment_text(recommendation["reason"]), templates="\n\n".join(template_blocks))
        raise IssueProcessingError("模板推荐未返回需要贡献者修改的有效结果")
    raise IssueProcessingError("规范性检查未返回需要贡献者修改的有效结果")
