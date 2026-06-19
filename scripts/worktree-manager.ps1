#!/usr/bin/env pwsh
# Worktree manager for develop-loop (Windows/PowerShell)
# Usage: pwsh -File scripts/worktree-manager.ps1 {create|assign|clean|preserve|list-preserved} <N> <slug> [reason]

param(
  [Parameter(Position=0, Mandatory=$true)]
  [ValidateSet("create","assign","clean","preserve","list-preserved")]
  [string]$Action,

  [Parameter(Position=1)]
  [string]$N = "",

  [Parameter(Position=2)]
  [string]$Slug = "",

  [Parameter(Position=3)]
  [string]$Reason = ""
)

$WorktreesDir = ".worktrees"
$StatusDir = ".status"

switch ($Action) {
  "create" {
    $Branch = "tmp/issue-${N}-${Slug}"
    $WorktreePath = "${WorktreesDir}\issue-${N}-${Slug}"
    New-Item -ItemType Directory -Path $WorktreesDir -Force | Out-Null
    New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null
    git worktree add -b $Branch $WorktreePath HEAD
    if ($?) {
      Write-Output $WorktreePath
    } else {
      Write-Error "Failed to create worktree for branch $Branch"
      exit 1
    }
  }
  "assign" {
    $WorktreePath = "${WorktreesDir}\issue-${N}-${Slug}"
    Write-Output $WorktreePath
  }
  "clean" {
    $WorktreePath = "${WorktreesDir}\issue-${N}-${Slug}"
    git worktree remove $WorktreePath 2>$null
    git branch -D "tmp/issue-${N}-${Slug}" 2>$null
  }
  "preserve" {
    $WorktreePath = "${WorktreesDir}\issue-${N}-${Slug}"
    New-Item -ItemType Directory -Path $StatusDir -Force | Out-Null
    $timestamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
    "${timestamp}: Preserved ${WorktreePath} for issue ${N} (${Slug}): ${Reason}" | Add-Content -Path "${StatusDir}\preserved-worktrees.log"
    Write-Output "$WorktreePath preserved"
  }
  "list-preserved" {
    if (Test-Path -LiteralPath "$StatusDir\preserved-worktrees.log") {
      Get-Content -LiteralPath "$StatusDir\preserved-worktrees.log"
    } else {
      Write-Output "No preserved worktrees"
    }
  }
}
