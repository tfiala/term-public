# term-public Neovim distribution config sources.
#
# Each row is alias|NVIM_APPNAME|public clone URL|configuration branch.
# setup.sh installs missing config checkouts, and bash/bashrc uses the same
# rows to define aliases.
TERM_PUBLIC_NVIM_CONFIG_SPECS=(
  "va|nvim-astro5|https://github.com/tfiala/nvim-astro5.git|tfiala"
  "vl|nvim-lazyvim|https://github.com/tfiala/nvim-config.git|tfiala"
  "vn|nvim-nvchad|https://github.com/tfiala/nvim-nvchad.git|tfiala"
)
