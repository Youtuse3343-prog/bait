(() => {
  'use strict';

  const q = (selector, root = document) => root.querySelector(selector);
  const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // Keep the sticky section navigation useful on long server pages.
  const nav = q('.server-subnav');
  if (nav && 'IntersectionObserver' in window) {
    const links = qa('a[href^="#"]', nav);
    const targets = links.map(link => q(link.getAttribute('href'))).filter(Boolean);
    const observer = new IntersectionObserver((entries) => {
      const visible = entries
        .filter(entry => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      links.forEach(link => link.classList.toggle('active', link.getAttribute('href') === `#${visible.target.id}`));
    }, { rootMargin: '-22% 0px -65% 0px', threshold: [0.05, 0.2, 0.5] });
    targets.forEach(target => observer.observe(target));
  }

  // Server-stat rows: only emphasize the role selector when Role Members is selected.
  const statsPanel = q('#server-stats');
  qa('[data-stat-row]').forEach((row) => {
    const type = q('[data-stat-type]', row);
    const roleField = q('.stat-role-field', row);
    const roleSelect = q('.stat-role-select', row);
    const preview = q('[data-stat-preview]', row);
    const emoji = q('input[name^="stat_emoji_"]', row);
    const label = q('input[name^="stat_label_"]', row);
    const template = q('input[name^="stat_template_"]', row);
    if (!type || !roleField) return;
    const countForType = () => {
      if (type.value === 'role') return roleSelect?.selectedOptions[0]?.dataset.count || '0';
      const map = {
        members: statsPanel?.dataset.membersCount,
        humans: statsPanel?.dataset.humansCount,
        bots: statsPanel?.dataset.botsCount,
        boosts: statsPanel?.dataset.boostsCount,
      };
      return map[type.value] || '0';
    };
    const refresh = () => {
      roleField.classList.toggle('is-muted', type.value !== 'role');
      if (!preview) return;
      let output = (template?.value || '{emoji} {label}: {value}')
        .replaceAll('{emoji}', emoji?.value || '')
        .replaceAll('{label}', label?.value || 'Stat')
        .replaceAll('{value}', countForType());
      preview.textContent = output.replace(/\s+/g, ' ').trim() || 'Configure this stat';
    };
    [type, roleSelect, emoji, label, template].filter(Boolean).forEach(control => {
      control.addEventListener('input', refresh);
      control.addEventListener('change', refresh);
    });
    refresh();
  });

  // Generic confirmation hook for destructive dashboard actions.
  qa('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const message = form.dataset.confirm || 'Are you sure?';
      if (!window.confirm(message)) event.preventDefault();
    });
  });

  const form = q('#message-builder-form');
  if (!form) return;

  const preview = {
    content: q('[data-preview-content]'),
    embed: q('[data-preview-embed]'),
    author: q('[data-preview-author]'),
    title: q('[data-preview-title]'),
    description: q('[data-preview-description]'),
    fields: q('[data-preview-fields]'),
    thumbnail: q('[data-preview-thumbnail]'),
    image: q('[data-preview-image]'),
    footer: q('[data-preview-footer]'),
  };

  const byName = (name) => q(`[name="${name}"]`, form);
  const value = (name) => (byName(name)?.value || '').trim();
  const checked = (name) => Boolean(byName(name)?.checked);
  const isUrl = (text) => /^https?:\/\//i.test(text || '');

  const setText = (element, text, fallback) => {
    if (!element) return;
    const actual = (text || '').trim();
    element.textContent = actual || fallback || '';
    element.classList.toggle('is-placeholder', !actual && Boolean(fallback));
    element.hidden = !actual && !fallback;
  };

  const setImage = (element, url) => {
    if (!element) return;
    if (isUrl(url)) {
      element.src = url;
      element.hidden = false;
    } else {
      element.removeAttribute('src');
      element.hidden = true;
    }
  };

  const updateFields = () => {
    if (!preview.fields) return;
    preview.fields.replaceChildren();
    qa('[data-embed-field-row]', form).forEach((row) => {
      const name = (q('[data-field-name]', row)?.value || '').trim();
      const fieldValue = (q('[data-field-value]', row)?.value || '').trim();
      if (!name || !fieldValue) return;
      const field = document.createElement('div');
      field.className = `preview-field${q('[data-field-inline]', row)?.checked ? ' inline' : ''}`;
      const strong = document.createElement('strong');
      const text = document.createElement('div');
      strong.textContent = name;
      text.textContent = fieldValue;
      field.append(strong, text);
      preview.fields.append(field);
    });
    preview.fields.hidden = preview.fields.children.length === 0;
  };

  const updatePreview = () => {
    const outside = value('action_content');
    setText(preview.content, outside, '');

    const useEmbed = checked('action_use_embed');
    if (preview.embed) preview.embed.hidden = !useEmbed;
    if (!useEmbed) return;

    setText(preview.author, value('embed_author_name'), '');
    setText(preview.title, value('embed_title'), 'Announcement title');
    setText(preview.description, value('embed_description'), 'Write your embed and the preview will update as you type.');
    setImage(preview.thumbnail, value('embed_thumbnail_url'));
    setImage(preview.image, value('embed_image_url'));

    const footerText = value('embed_footer_text');
    const timestamp = checked('embed_timestamp');
    const footer = [footerText, timestamp ? 'Today at 12:00 PM' : ''].filter(Boolean).join(' • ');
    setText(preview.footer, footer, '');
    updateFields();
  };

  const colorPicker = q('[data-color-picker]', form);
  const colorText = q('[data-color-text]', form);
  const normalizeHex = (raw) => {
    let hex = (raw || '').trim();
    if (!hex.startsWith('#')) hex = `#${hex}`;
    if (/^#[0-9a-f]{6}$/i.test(hex)) return hex.toLowerCase();
    if (/^#[0-9a-f]{3}$/i.test(hex)) return `#${hex.slice(1).split('').map(ch => ch + ch).join('')}`.toLowerCase();
    return null;
  };
  const applyColor = (raw) => {
    const hex = normalizeHex(raw) || '#7c3aed';
    if (preview.embed) preview.embed.style.borderLeftColor = hex;
    return hex;
  };
  colorPicker?.addEventListener('input', () => {
    if (colorText) colorText.value = colorPicker.value;
    applyColor(colorPicker.value);
  });
  colorText?.addEventListener('input', () => {
    const hex = normalizeHex(colorText.value);
    if (hex && colorPicker) colorPicker.value = hex;
    if (hex) applyColor(hex);
  });

  // Reveal up to ten embed fields without making the initial form overwhelming.
  const addField = q('#add-embed-field');
  addField?.addEventListener('click', () => {
    const next = qa('[data-embed-field-row].is-hidden', form)[0];
    if (next) {
      next.classList.remove('is-hidden');
      q('input,textarea', next)?.focus();
    }
    if (!qa('[data-embed-field-row].is-hidden', form).length) addField.disabled = true;
  });

  form.addEventListener('input', updatePreview);
  form.addEventListener('change', updatePreview);
  applyColor(colorText?.value || '#7c3aed');
  updatePreview();
})();
