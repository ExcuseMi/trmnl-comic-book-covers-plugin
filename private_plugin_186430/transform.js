function transform(input) {
  const items = Array.isArray(input?.results) ? input.results : [];

  const pick = (arr) => arr.map(item => ({
    id: item.id,
    url: item.image?.small_url || item.image?.medium_url || item.image?.original_url || '',
    issue_number: item.issue_number || '',
    volume_name: item.volume?.name || ''
  }));

  const portrait_items = pick(items.filter(item =>
    item.image?.small_url || item.image?.medium_url || item.image?.original_url
  ));

  return {
    data: {
      portrait_items,
      portrait_count: portrait_items.length
    }
  };
}
