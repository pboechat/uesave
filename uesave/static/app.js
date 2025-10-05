const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const browseBtn = document.getElementById('browseBtn');
const statusEl = document.getElementById('status');
const statsEl = document.getElementById('stats');
const treeEl = document.getElementById('tree');
const pageOverlay = document.getElementById('page-drop-overlay');

function setStatus(msg, isError = false) {
    statusEl.textContent = msg || '';
    statusEl.style.color = isError ? '#ffb4b4' : 'var(--muted)';
}

function renderStats(header) {
    statsEl.innerHTML = '';
    const pairs = [
        ['Magic', header?.magic ?? ''],
        ['SaveGame Version', header?.save_game_version ?? header?.version ?? ''],
        ['Package File Version', header?.package_file_version ?? ''],
        ['SaveGame Class', header?.save_game_class_name ?? ''],
    ];
    for (const [k, v] of pairs) {
        const div = document.createElement('div');
        div.className = 'stat';
        div.innerHTML = `<div class="muted">${k}</div><div><b>${String(v)}</b></div>`;
        statsEl.appendChild(div);
    }
}

function iconSrcForType(type) {
    // Map property types to PNG icon filenames in /static
    const map = {
        'ArrayProperty': '/static/array_prop.png',
        'BoolProperty': '/static/bool_prop.png',
        'ByteProperty': '/static/byte_prop.png',
        'DoubleProperty': '/static/double_prop.png',
        'FloatProperty': '/static/float_prop.png',
        'IntProperty': '/static/int_prop.png',
        'Int64Property': '/static/int_prop.png',
        'UInt64Property': '/static/int_prop.png',
        'MapProperty': '/static/map_prop.png',
        'NameProperty': '/static/name_prop.png',
        'ObjectProperty': '/static/object_prop.png',
        'StrProperty': '/static/str_prop.png',
        'StructProperty': '/static/struct_prop.png',
        'TextProperty': '/static/text_prop.png',
    };
    return map[type] || '/static/object_prop.png';
}

function makeTreeItem(node) {
    const li = document.createElement('li');
    li.className = 'tree-item';
    const label = document.createElement('div');
    label.className = 'label';
        const icon = document.createElement('span');
        icon.className = 'icon';
        const img = document.createElement('img');
        img.className = 'icon-img';
        img.src = iconSrcForType(node.type);
        img.alt = node.type || 'Property';
        icon.appendChild(img);
        const text = document.createElement('span');
        text.className = 'text';
        const name = node.name || node.type || 'Property';
        const meta = node.meta || '';
        text.textContent = name;
        if (meta) label.title = meta;
        label.appendChild(icon);
        label.appendChild(text);
    li.appendChild(label);

    if (node.children && node.children.length) {
        const children = document.createElement('ul');
        children.className = 'children';
        for (const child of node.children) {
            children.appendChild(makeTreeItem(child));
        }
        children.style.display = 'none';
        label.addEventListener('click', () => {
            const isHidden = children.style.display === 'none';
            children.style.display = isHidden ? 'block' : 'none';
        });
        li.appendChild(children);
    } else if (node.value !== undefined && node.value !== null) {
        // Leaf with a displayable value: toggle a synthetic single child on click
        const children = document.createElement('ul');
        children.className = 'children';
        const childLi = document.createElement('li');
        childLi.className = 'tree-item';
        const childLabel = document.createElement('div');
        childLabel.className = 'label';
        const icon = document.createElement('span');
        icon.className = 'icon';
        const img = document.createElement('img');
        img.className = 'icon-img';
        // Always use the equals icon for value nodes
        img.src = '/static/equals.png';
        img.alt = 'Value';
        icon.appendChild(img);
        const text = document.createElement('span');
        text.className = 'text';
        text.textContent = String(node.value);
        childLabel.appendChild(icon);
        childLabel.appendChild(text);
        childLi.appendChild(childLabel);
        children.appendChild(childLi);
        children.style.display = 'none';
        label.addEventListener('click', () => {
            const isHidden = children.style.display === 'none';
            children.style.display = isHidden ? 'block' : 'none';
        });
        li.appendChild(children);
    }
    return li;
}

function renderTree(nodes) {
    treeEl.innerHTML = '';
        for (const n of nodes) treeEl.appendChild(makeTreeItem(n));
}

async function upload(file) {
    setStatus('Uploading...');
    const fd = new FormData();
    fd.append('file', file);
    try {
        const res = await fetch('/api/upload', { method: 'POST', body: fd });
        if (!res.ok) throw new Error('Upload failed');
        const data = await res.json();
        renderStats(data.header || {});
        renderTree(data.properties || []);
        setStatus('Done');
    } catch (e) {
        console.error(e);
        setStatus('Error: ' + (e?.message || e), true);
    }
}

// Events specific to the small dropzone widget
if (dropzone && fileInput && browseBtn) {
    dropzone.addEventListener('click', () => fileInput.click());
    // Prevent the dropzone's click handler from also firing when clicking the browse button
    browseBtn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); fileInput.click(); });
    fileInput.addEventListener('change', (e) => {
        const f = e.target.files?.[0];
        if (f) upload(f);
    });

    dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragover'); });
    dropzone.addEventListener('dragleave', e => { e.preventDefault(); dropzone.classList.remove('dragover'); });
    dropzone.addEventListener('drop', e => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        const file = e.dataTransfer.files?.[0];
        if (file) upload(file);
    });
}

// Page-wide drag-and-drop support with overlay
let dragCounter = 0;
window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    if (pageOverlay) pageOverlay.classList.add('show');
});
window.addEventListener('dragover', (e) => {
    e.preventDefault();
});
window.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0 && pageOverlay) pageOverlay.classList.remove('show');
});
window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    if (pageOverlay) pageOverlay.classList.remove('show');
    const file = e.dataTransfer?.files?.[0];
    if (file) upload(file);
});
